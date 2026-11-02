# =====================================================================
#  Regador Inteligente v2 - servidor (PythonAnywhere, HTTP porta 80)
# =====================================================================
#  O Arduino (ESP-01 com firmware AT) chama GET /d a cada ciclo.
#  A resposta e UMA linha de texto com a configuracao, o comando da fila
#  (se houver) e quantas leituras antigas foram recebidas (hq):
#     CFG;i=10;lo=34;hi=65;m=0;v=-1;vm=-1;cv=3;c=1;cid=17;cd=5;hq=12;FIM
#
#  Rede de usuarios (painel web):
#   - todos veem tudo e todos podem pedir rega / parar
#   - pedidos de rega entram numa FILA e sao entregues um por vez
#   - toda acao vira um EVENTO que aparece para todos os conectados
#   - mudar configuracao trava ate o Arduino confirmar
#   - protecao contra encharcar: so prossegue com senha
#   - historico guardado por 7 dias
# =====================================================================

import os, json, time, sqlite3, csv, io
from datetime import datetime, timezone, timedelta
from flask import Flask, request, Response, jsonify, send_from_directory

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("America/Sao_Paulo")
except Exception:  # sem base de fusos: usa -3h fixo
    TZ = timezone(timedelta(hours=-3))

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "regador.db")

TOKEN = os.environ.get("DEVICE_TOKEN", "irriga123")  # igual ao TOKEN do Arduino
SENHA = os.environ.get("SENHA_PAINEL", "fatec2026")  # senha das acoes de risco

# Padroes = valores do seu codigo original (rega manual: 5 s)
CFG_PADRAO = {"intervalo": 10, "lo": 34, "hi": 65, "modo": 0, "vel": -1, "velm": -1,
              "duracao": 5, "protecao": 30}
LIMITES = {"intervalo": (5, 600), "lo": (0, 99), "hi": (1, 100), "modo": (0, 2),
           "vel": (-1, 255), "velm": (-1, 255), "duracao": (2, 600), "protecao": (0, 1440)}
CAMPOS_ARDUINO = ("intervalo", "lo", "hi", "modo", "vel", "velm")  # o Arduino precisa confirmar
NOMES_CAMPOS = {"intervalo": "envio (s)", "lo": "liga abaixo de (%)", "hi": "desliga a partir de (%)",
                "modo": "modo", "vel": "potência automática", "velm": "potência manual",
                "duracao": "rega manual (s)", "protecao": "proteção (min)"}
DURACAO_RECOMENDADA = 10  # acima disso o painel pede confirmacao

MODOS = ["automático", "manual", "desligado"]
MOTIVOS = {0: "desligada", 1: "automática (histerese)", 2: "manual (pedido)", -1: "registrada offline"}
LEDS = ["vermelho (seco)", "amarelo (meio úmido)", "verde (molhado)"]
CMD_COD = {"regar": 1, "parar": 2, "retomar": 3}
FALHAS = {
    0: "ok", 1: "ESP não responde ao AT", 2: "falha ao conectar no Wi-Fi",
    3: "falha ao abrir TCP (DNS/servidor/internet)", 4: "CIPSEND recusado",
    5: "envio não confirmado (SEND OK)", 6: "servidor não respondeu a tempo",
    7: "servidor recusou (token/dados)", 8: "resposta incompleta/inválida",
}
ERROS_SENSOR = {1: "temperatura fora da faixa (TMP36)", 2: "sensor de solo instável/desconectado",
                4: "LDR com leitura instável"}

RETENCAO_S = 7 * 24 * 3600  # historico: ultimos 7 dias
LOCK_CFG_S = 120  # trava de configuracao expira se o Arduino nao confirmar
PRESENCA_S = 15  # "conectado" se consultou nos ultimos 15 s
RAM_TOTAL, FLASH_TOTAL = 2048, 32256  # ATmega328P (flash sem o bootloader)

app = Flask(__name__)


# ---------------------------------------------------------------------
#  Banco (cria e atualiza bancos antigos sem perder dados)
# ---------------------------------------------------------------------
def db():
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    return con


ESQUEMA = {
    "leituras": "id INTEGER PRIMARY KEY, em REAL, origem TEXT, temp REAL, ldr INT, solo_raw INT, solo INT, "
                "pot INT, vel INT, bomba INT, motivo INT, led INT, modo INT, erros INT, "
                "ram INT, carga INT, lmx INT, vcc INT",
    "eventos": "id INTEGER PRIMARY KEY, em REAL, tipo TEXT, quem TEXT, msg TEXT",
    "comunicacao": "id INTEGER PRIMARY KEY, em REAL, direcao TEXT, resumo TEXT",
    "pedidos": "id INTEGER PRIMARY KEY, criado REAL, tipo TEXT, duracao INT, quem TEXT, prioridade INT, "
               "status TEXT, forcado INT DEFAULT 0, obs TEXT, entregue REAL, executado REAL, vel INT",
    "regas": "id INTEGER PRIMARY KEY, inicio REAL, fim REAL, motivo INT, quem TEXT, origem TEXT, "
             "vel INT, solo_ini INT, solo_fim INT, temp_ini REAL, duracao_pedida INT, lo INT, hi INT",
    "kv": "k TEXT PRIMARY KEY, v TEXT",
    "presenca": "nome TEXT PRIMARY KEY, visto REAL",
}


def init_db():
    with db() as con:
        for tabela, colunas in ESQUEMA.items():
            con.execute(f"CREATE TABLE IF NOT EXISTS {tabela}({colunas})")
            existentes = {r["name"] for r in con.execute(f"PRAGMA table_info({tabela})")}
            for col in colunas.split(","):
                partes = col.strip().split()
                if partes[0] not in existentes and "PRIMARY" not in col:
                    con.execute(f"ALTER TABLE {tabela} ADD COLUMN {' '.join(partes)}")
        for tabela, campo in (("leituras", "em"), ("eventos", "em"), ("comunicacao", "em"),
                              ("pedidos", "criado"), ("regas", "inicio")):
            con.execute(f"CREATE INDEX IF NOT EXISTS ix_{tabela}_{campo} ON {tabela}({campo})")


def kv_get(con, k, padrao=None):
    r = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return json.loads(r["v"]) if r else padrao


def kv_set(con, k, v):
    con.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, json.dumps(v)))


def kv_del(con, k):
    con.execute("DELETE FROM kv WHERE k=?", (k,))


def evento(con, tipo, msg, quem=None, em=None):
    con.execute("INSERT INTO eventos(em,tipo,quem,msg) VALUES(?,?,?,?)", (em or time.time(), tipo, quem, msg))


def comunicacao(con, direcao, resumo):
    con.execute("INSERT INTO comunicacao(em,direcao,resumo) VALUES(?,?,?)", (time.time(), direcao, resumo))


def limpar_antigos(con, agora):
    """Apaga o que tem mais de 7 dias (roda no maximo a cada 10 min)."""
    if agora - kv_get(con, "ultima_limpeza", 0) < 600:
        return
    corte = agora - RETENCAO_S
    for tabela, campo in (("leituras", "em"), ("eventos", "em"), ("comunicacao", "em"),
                          ("pedidos", "criado"), ("regas", "inicio")):
        con.execute(f"DELETE FROM {tabela} WHERE {campo} < ?", (corte,))
    con.execute("DELETE FROM presenca WHERE visto < ?", (agora - 86400,))
    kv_set(con, "ultima_limpeza", agora)


def get_cfg(con):
    cfg = dict(CFG_PADRAO)
    cfg.update({k: v for k, v in kv_get(con, "config", {}).items() if k in CFG_PADRAO})
    cfg["versao"] = kv_get(con, "cfg_versao", 1)
    return cfg


def validar_cfg(base, novo):
    cfg = {k: base[k] for k in CFG_PADRAO}
    for chave, (mn, mx) in LIMITES.items():
        if chave in novo and str(novo[chave]).strip() != "":
            try:
                val = int(float(novo[chave]))
            except (TypeError, ValueError):
                return None, f"'{NOMES_CAMPOS[chave]}' inválido"
            if not mn <= val <= mx:
                return None, f"'{NOMES_CAMPOS[chave]}' deve ficar entre {mn} e {mx}"
            cfg[chave] = val
    if cfg["lo"] >= cfg["hi"]:
        return None, "O limite que LIGA precisa ser menor que o limite que DESLIGA"
    return cfg, None


def nome_valido(n):
    n = (n or "").strip()[:24]
    return n or None


def senha_ok(s):
    return s is not None and s == SENHA


def online_ate(cfg):
    return max(3 * cfg["intervalo"], 30)


def hoje_inicio():
    agora = datetime.now(TZ)
    return agora.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def txt_vel(v):
    return "potenciômetro" if v is None or v < 0 else f"{v}/255 ({round(v * 100 / 255)}%)"


# ---------------------------------------------------------------------
#  Regras de negocio
# ---------------------------------------------------------------------
def fila_ativa(con):
    return con.execute("""SELECT * FROM pedidos WHERE status IN ('entregue','fila')
                          ORDER BY CASE status WHEN 'entregue' THEN 0 ELSE 1 END, prioridade DESC, id""").fetchall()


def ultima_leitura(con):
    return con.execute("SELECT * FROM leituras WHERE origem='ao vivo' ORDER BY id DESC LIMIT 1").fetchone()


def protecao(con, cfg, agora):
    motivos, ate = [], None
    ult = con.execute("SELECT * FROM regas ORDER BY id DESC LIMIT 1").fetchone()
    janela = cfg["protecao"] * 60
    if ult:
        if ult["fim"] is None:
            motivos.append("A bomba está ligada agora")
        elif janela and agora - ult["fim"] < janela:
            mins = int((agora - ult["fim"]) // 60)
            motivos.append(f"Já houve uma rega há {mins} min (proteção de {cfg['protecao']} min)")
            ate = ult["fim"] + janela
    leit = ultima_leitura(con)
    if leit and leit["solo"] is not None and leit["solo"] >= cfg["hi"]:
        motivos.append(f"O solo já está úmido ({leit['solo']}% ≥ {cfg['hi']}%)")
    n = con.execute("SELECT COUNT(*) FROM pedidos WHERE tipo='regar' AND status IN ('fila','entregue')").fetchone()[0]
    if n:
        motivos.append(f"Já existe{'m' if n > 1 else ''} {n} rega{'s' if n > 1 else ''} na fila")
    return motivos, ate


def descrever_pedido(p):
    if p["tipo"] == "regar":
        return f"rega de {p['duracao']} s"
    return {"parar": "parar a bomba", "retomar": "retomar o automático"}.get(p["tipo"], p["tipo"])


def lock_cfg(con):
    lock = kv_get(con, "lock_cfg")
    if not lock:
        return None
    lock["expirado"] = time.time() - lock["desde"] > LOCK_CFG_S
    return lock


def quem_ocupa(con):
    lock = lock_cfg(con)
    if lock and not lock["expirado"]:
        return {"quem": lock["quem"], "o_que": f"uma configuração (v{lock['versao']})"}
    fila = fila_ativa(con)
    if fila:
        return {"quem": fila[0]["quem"], "o_que": descrever_pedido(fila[0])}
    return None


def registrar_bomba(con, em, leit, motivo, origem, cfg):
    """Cria/fecha registros de rega a partir das transicoes da bomba, com a configuracao usada."""
    antes = kv_get(con, "bomba_estado", 0)
    ligada = bool(leit.get("bomba"))
    if ligada and not antes:
        quem = dur_pedida = None
        if motivo == 2:
            p = con.execute(
                "SELECT quem, duracao FROM pedidos WHERE tipo='regar' AND status IN ('entregue','executado') "
                "ORDER BY id DESC LIMIT 1").fetchone()
            if p:
                quem, dur_pedida = p["quem"], p["duracao"]
        vel = leit.get("vel")
        con.execute("""INSERT INTO regas(inicio,motivo,quem,origem,vel,solo_ini,temp_ini,duracao_pedida,lo,hi)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (em, motivo, quem, origem, vel, leit.get("solo"), leit.get("temp"), dur_pedida, cfg["lo"],
                     cfg["hi"]))
        partes = [f"Bomba LIGOU — rega {MOTIVOS.get(motivo, '?')}"]
        if quem:
            partes.append(f"pedida por {quem} ({dur_pedida} s)")
        if motivo == 1:
            partes.append(f"liga < {cfg['lo']}%, desliga ≥ {cfg['hi']}%")
        if vel is not None:
            partes.append(f"potência {vel}/255")
        if leit.get("solo") is not None:
            partes.append(f"solo {leit['solo']}%")
        if origem == "buffer":
            partes.append("registrada enquanto estava offline")
        evento(con, "rega", " · ".join(partes), quem, em)
    elif ligada and antes:
        # guarda a potencia se ainda nao tinha (ex.: rega comecou numa leitura do buffer)
        if leit.get("vel"):
            con.execute("UPDATE regas SET vel=? WHERE fim IS NULL AND vel IS NULL", (leit["vel"],))
    elif not ligada and antes:
        r = con.execute("SELECT * FROM regas WHERE fim IS NULL ORDER BY id DESC LIMIT 1").fetchone()
        txt = "Bomba DESLIGOU"
        if r:
            con.execute("UPDATE regas SET fim=?, solo_fim=? WHERE id=?", (em, leit.get("solo"), r["id"]))
            txt += f" após {int(em - r['inicio'])} s"
            if leit.get("solo") is not None and r["solo_ini"] is not None:
                txt += f" · solo {r['solo_ini']}% → {leit['solo']}%"
        evento(con, "rega", txt, None, em)
    kv_set(con, "bomba_estado", 1 if ligada else 0)


def pedido_manual_recente(con):
    return con.execute("SELECT quem, duracao FROM pedidos WHERE tipo='regar' AND status IN ('entregue','executado') "
                       "ORDER BY id DESC LIMIT 1").fetchone()


def abrir_rega_ao_vivo(con, em, leit, cfg):
    """Bomba ligada numa leitura ao vivo e nenhuma rega aberta: cria o registro (em andamento)."""
    if con.execute("SELECT 1 FROM regas WHERE fim IS NULL").fetchone():
        if leit.get("vel"):
            con.execute("UPDATE regas SET vel=? WHERE fim IS NULL", (leit["vel"],))
        return
    motivo = leit.get("motivo")
    quem = dur_pedida = None
    if motivo == 2:
        p = pedido_manual_recente(con)
        if p:
            quem, dur_pedida = p["quem"], p["duracao"]
    con.execute("""INSERT INTO regas(inicio,motivo,quem,origem,vel,solo_ini,temp_ini,duracao_pedida,lo,hi)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (em, motivo, quem, "ao vivo", leit.get("vel"), leit.get("solo"), leit.get("temp"), dur_pedida,
                 cfg["lo"], cfg["hi"]))
    partes = [f"Bomba LIGOU — rega {MOTIVOS.get(motivo, '?')}"]
    if quem:
        partes.append(f"pedida por {quem} ({dur_pedida} s)")
    if motivo == 1:
        partes.append(f"liga < {cfg['lo']}%, desliga ≥ {cfg['hi']}%")
    partes.append(f"potência {leit.get('vel')}/255 · solo {leit.get('solo')}%")
    evento(con, "rega", " · ".join(partes), quem, em)


def processar_registro_regas(con, texto_regs, up, agora, leit, cfg):
    """Registro enviado pelo Arduino v2 com as ultimas regas concluidas (inclusive as curtas
    que comecam e terminam entre dois envios). Cada rega tem um id; repetidas sao ignoradas."""
    ctrl = kv_get(con, "regas_ctrl", {"max": 0, "up": 0})
    if up + 5 < ctrl.get("up", 0):  # Arduino reiniciou: ids recomecam do 1
        ctrl["max"] = 0
    novas = []
    for item in (texto_regs or "").split("_"):
        p = item.split(",")
        if len(p) != 7:
            continue
        try:
            rid, idade, dur, motivo, vel, si, sf = map(int, p)
        except ValueError:
            continue
        if rid > ctrl["max"]:
            novas.append((rid, idade, dur, motivo, vel, si, sf))
    for rid, idade, dur, motivo, vel, si, sf in sorted(novas):
        inicio = agora - idade
        fim = inicio + dur
        quem = dur_pedida = None
        if motivo == 2:
            pm = pedido_manual_recente(con)
            if pm:
                quem, dur_pedida = pm["quem"], pm["duracao"]
        aberta = con.execute("SELECT * FROM regas WHERE fim IS NULL ORDER BY id DESC LIMIT 1").fetchone()
        if aberta:
            con.execute("""UPDATE regas SET inicio=?, fim=?, motivo=?, vel=?, solo_ini=?, solo_fim=?,
                           quem=COALESCE(quem,?), duracao_pedida=COALESCE(duracao_pedida,?) WHERE id=?""",
                        (inicio, fim, motivo, vel, si, sf, quem, dur_pedida, aberta["id"]))
        else:
            con.execute("""INSERT INTO regas(inicio,fim,motivo,quem,origem,vel,solo_ini,solo_fim,temp_ini,
                           duracao_pedida,lo,hi) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (inicio, fim, motivo, quem, "registro", vel, si, sf, leit.get("temp"), dur_pedida,
                         cfg["lo"], cfg["hi"]))
        tipo = MOTIVOS.get(motivo, "?")
        txt = f"Rega {tipo} concluída: {dur} s · potência {vel}/255 ({round(vel * 100 / 255)}%) · solo {si}% → {sf}%"
        if quem:
            txt += f" · pedida por {quem}"
        evento(con, "rega", txt, quem, fim)
        ctrl["max"] = rid
    ctrl["up"] = up
    kv_set(con, "regas_ctrl", ctrl)
    # bomba desligada mas ficou rega aberta (registro perdido): fecha agora
    if not leit.get("bomba"):
        con.execute("UPDATE regas SET fim=?, solo_fim=COALESCE(solo_fim,?) WHERE fim IS NULL",
                    (agora, leit.get("solo")))
    else:
        abrir_rega_ao_vivo(con, agora, leit, cfg)


def avaliar_hw(d):
    """Diz se o Arduino esta bem, em atencao ou sobrecarregado."""
    if not d or "lmx" not in d:
        return {"nivel": -1, "status": "sem dados",
                "motivos": ["O Arduino ainda não enviou dados de hardware (sketch antigo?)"]}
    nivel, mot = 0, []
    lmx_ms = d.get("lmx", 0) / 1000
    if lmx_ms > 50:
        nivel = max(nivel, 2);
        mot.append(f"Volta mais lenta do loop {lmx_ms:.1f} ms (> 50 ms): a serial do ESP pode perder dados")
    elif lmx_ms > 25:
        nivel = max(nivel, 1);
        mot.append(f"Volta mais lenta do loop {lmx_ms:.1f} ms (atenção acima de 25 ms)")
    if d.get("ocu", 0) > 80:
        nivel = max(nivel, 2);
        mot.append(f"Carga média {d['ocu']}% (> 80%)")
    elif d.get("ocu", 0) > 50:
        nivel = max(nivel, 1);
        mot.append(f"Carga média {d['ocu']}% (atenção acima de 50%)")
    if d.get("rmn") and d["rmn"] < 150:
        nivel = max(nivel, 2);
        mot.append(f"RAM livre mínima {d['rmn']} bytes (< 150)")
    elif d.get("rmn") and d["rmn"] < 300:
        nivel = max(nivel, 1);
        mot.append(f"RAM livre mínima {d['rmn']} bytes (atenção abaixo de 300)")
    if d.get("vcc") and d["vcc"] < 4300:
        nivel = max(nivel, 2);
        mot.append(f"Alimentação {d['vcc'] / 1000:.2f} V (< 4,30 V)")
    elif d.get("vcc") and d["vcc"] < 4600:
        nivel = max(nivel, 1);
        mot.append(f"Alimentação {d['vcc'] / 1000:.2f} V (atenção abaixo de 4,60 V)")
    if d.get("qc") and d.get("buffer", 0) >= 0.75 * d["qc"]:
        nivel = max(nivel, 1);
        mot.append(f"Buffer quase cheio ({d['buffer']}/{d['qc']})")
    if not mot:
        mot.append("Tudo dentro dos limites seguros")
    return {"nivel": nivel, "status": ["ok", "atenção", "sobrecarregado"][nivel], "motivos": mot}


# ---------------------------------------------------------------------
#  Rota do dispositivo
# ---------------------------------------------------------------------
def texto(s, status=200):
    return Response(s, status=status, mimetype="text/plain")


@app.route("/d")
def dispositivo():
    q = request.args
    if q.get("k") != TOKEN:
        return texto("ERR;token\n", 401)

    def gi(nome, padrao=0):
        try:
            return int(q.get(nome, padrao))
        except (TypeError, ValueError):
            return padrao

    agora = time.time()
    con = db()
    try:
        with con:
            cfg = get_cfg(con)
            disp_antes = kv_get(con, "dispositivo", {})
            visto_antes = disp_antes.get("visto", 0)

            # ---- leituras antigas que estavam no buffer do Arduino ----
            hist = []
            for item in (q.get("h") or "").split("_"):
                p = item.split(",")
                if len(p) != 5:
                    continue
                try:
                    idade, t10, ldr, solo, fl = map(int, p)
                except ValueError:
                    continue
                hist.append({"em": agora - max(idade, 0), "temp": t10 / 10, "ldr": ldr, "solo": solo,
                             "bomba": fl & 1, "erros": fl >> 1})
            hist.sort(key=lambda x: x["em"])
            sketch_v2 = "rgs" in q  # o sketch novo manda o registro de regas
            for h in hist:
                con.execute("INSERT INTO leituras(em,origem,temp,ldr,solo,bomba,erros) VALUES(?,?,?,?,?,?,?)",
                            (h["em"], "buffer", h["temp"], h["ldr"], h["solo"], h["bomba"], h["erros"]))
                if not sketch_v2:
                    registrar_bomba(con, h["em"], h, -1, "buffer", cfg)

            # ---- leitura atual (todos os sensores + hardware) ----
            cur = {"temp": gi("t") / 10, "ldr": gi("l"), "solo_raw": gi("sr"), "solo": gi("s"),
                   "pot": gi("p"), "vel": gi("v"), "bomba": gi("b"), "motivo": gi("bm"),
                   "led": gi("led"), "modo": gi("m"), "erros": gi("e"),
                   "ram": gi("ram", None), "carga": gi("ocu", None), "lmx": gi("lmx", None), "vcc": gi("vcc", None)}
            con.execute("""INSERT INTO leituras(em,origem,temp,ldr,solo_raw,solo,pot,vel,bomba,motivo,led,modo,erros,
                           ram,carga,lmx,vcc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (agora, "ao vivo", cur["temp"], cur["ldr"], cur["solo_raw"], cur["solo"], cur["pot"],
                         cur["vel"], cur["bomba"], cur["motivo"], cur["led"], cur["modo"], cur["erros"],
                         cur["ram"], cur["carga"], cur["lmx"], cur["vcc"]))
            if sketch_v2:
                processar_registro_regas(con, q.get("rgs"), gi("up"), agora, cur, cfg)
            else:
                registrar_bomba(con, agora, cur, cur["motivo"], "ao vivo", cfg)

            # ---- estado interno e hardware do Arduino ----
            disp = {"visto": agora, "up": gi("up"), "falha": gi("f"), "nfalhas": gi("n"), "buffer": gi("q"),
                    "descartadas": gi("x"), "regas": gi("rg"), "bomba_s": gi("lig"), "ram": gi("ram"),
                    "atrasos": gi("atr"), "cv": gi("cv"), "ack": gi("a"), "restante": gi("rr"),
                    "pausa": gi("pz"), "hist_recebido": len(hist),
                    "ip": request.headers.get("X-Real-IP", request.remote_addr)}
            for k in ("qc", "env", "rmn", "rst", "fl", "vcc", "mhz", "lps", "lpn", "lmx", "ocu", "ocm"):
                if k in q:
                    disp[k] = gi(k)
            if visto_antes and agora - visto_antes > online_ate(cfg):
                evento(con, "online", "Regador voltou a ficar ONLINE")
            elif not visto_antes:
                evento(con, "online", "Regador conectou pela primeira vez")
            if disp_antes and disp["up"] + 5 < disp_antes.get("up", 0):
                evento(con, "alerta", "O Arduino reiniciou (o contador de tempo ligado zerou)")
            if disp["falha"]:
                evento(con, "alerta", f"Arduino voltou após {disp['nfalhas']} falha(s). Última: "
                                      f"{FALHAS.get(disp['falha'], disp['falha'])}. Enviou {len(hist)} leitura(s) guardada(s).")
            if cur["erros"] != disp_antes.get("erros", 0):
                if cur["erros"]:
                    evento(con, "alerta", "Sensor com problema: " +
                           "; ".join(t for b, t in ERROS_SENSOR.items() if cur["erros"] & b))
                else:
                    evento(con, "info", "Sensores normalizados")
            disp["erros"] = cur["erros"]
            hw = avaliar_hw(disp)
            if hw["nivel"] != disp_antes.get("hw_nivel", 0) and hw["nivel"] >= 0:
                if hw["nivel"] == 2:
                    evento(con, "alerta", "Arduino SOBRECARREGADO: " + "; ".join(hw["motivos"]))
                elif hw["nivel"] == 1 and disp_antes.get("hw_nivel", 0) == 0:
                    evento(con, "info", "Arduino em atenção: " + "; ".join(hw["motivos"]))
                elif hw["nivel"] == 0:
                    evento(con, "info", "Hardware do Arduino normalizado")
            disp["hw_nivel"] = hw["nivel"]
            kv_set(con, "dispositivo", disp)
            kv_set(con, "online_flag", True)

            # ---- confirmacoes do Arduino ----
            if disp["ack"]:
                p = con.execute("SELECT * FROM pedidos WHERE id=? AND status='entregue'", (disp["ack"],)).fetchone()
                if p:
                    con.execute("UPDATE pedidos SET status='executado', executado=? WHERE id=?", (agora, p["id"]))
                    evento(con, "confirmacao", f"Arduino executou: {descrever_pedido(p)} (pedido de {p['quem']})",
                           p["quem"])
            lock = kv_get(con, "lock_cfg")
            if lock and disp["cv"] >= lock["versao"]:
                kv_del(con, "lock_cfg")
                evento(con, "confirmacao",
                       f"Arduino aplicou a configuração v{lock['versao']} enviada por {lock['quem']}",
                       lock["quem"])

            # ---- escolhe o comando a entregar (um por vez) ----
            pend = con.execute("SELECT * FROM pedidos WHERE status='entregue' ORDER BY id LIMIT 1").fetchone()
            if not pend:
                for p in con.execute("SELECT * FROM pedidos WHERE status='fila' ORDER BY prioridade DESC, id"):
                    if p["tipo"] == "regar" and (disp["restante"] > 0 or cfg["modo"] == 2):
                        continue
                    pend = p
                    break
                if pend:
                    con.execute("UPDATE pedidos SET status='entregue', entregue=? WHERE id=?", (agora, pend["id"]))
                    evento(con, "entrega", f"Enviado ao Arduino: {descrever_pedido(pend)} (pedido de {pend['quem']})",
                           pend["quem"])

            c = cid = cd = 0
            if pend:
                c, cid, cd = CMD_COD.get(pend["tipo"], 0), pend["id"], pend["duracao"] or 0
            linha = (f"CFG;i={cfg['intervalo']};lo={cfg['lo']};hi={cfg['hi']};m={cfg['modo']};v={cfg['vel']};"
                     f"vm={cfg['velm']};cv={cfg['versao']};c={c};cid={cid};cd={cd};hq={len(hist)};FIM\n")

            comunicacao(con, "recebido",
                        f"T={cur['temp']:.1f}C solo={cur['solo']}% (raw {cur['solo_raw']}) LDR={cur['ldr']} "
                        f"pot={cur['pot']} bomba={'ON' if cur['bomba'] else 'off'} vel={cur['vel']} "
                        f"| +{len(hist)} do buffer (restam {disp['buffer']}) | RAM livre {disp.get('ram')} "
                        f"| carga {disp.get('ocu', '?')}% | ack={disp['ack']} cv={disp['cv']}")
            comunicacao(con, "enviado", linha.strip())
            limpar_antigos(con, agora)
        return texto(linha)
    except Exception as e:  # nunca deixa o Arduino sem resposta
        app.logger.exception("erro em /d")
        return texto(f"ERR;servidor {type(e).__name__}\n", 500)
    finally:
        con.close()


# ---------------------------------------------------------------------
#  API do painel
# ---------------------------------------------------------------------
def corpo():
    return request.get_json(silent=True) or {}


def erro(msg, status=400, **extra):
    return jsonify(ok=False, erro=msg, **extra), status


def linha_dict(r):
    return dict(r) if r else None


def regas_com_config(rows, agora):
    out = []
    for r in rows:
        d = dict(r)
        d["duracao_real"] = int((d["fim"] or agora) - d["inicio"])
        d["em_andamento"] = d["fim"] is None
        out.append(d)
    return out


@app.route("/api/estado")
def api_estado():
    agora = time.time()
    nome = nome_valido(request.args.get("nome"))
    try:
        desde = int(request.args.get("desde", 0))
    except ValueError:
        desde = 0
    con = db()
    try:
        with con:
            cfg = get_cfg(con)
            if nome:
                con.execute("INSERT OR REPLACE INTO presenca(nome,visto) VALUES(?,?)", (nome, agora))
            disp = kv_get(con, "dispositivo", {})
            online = bool(disp) and agora - disp.get("visto", 0) < online_ate(cfg)
            if not online and kv_get(con, "online_flag", False):
                kv_set(con, "online_flag", False)
                evento(con, "offline", "Regador ficou OFFLINE (sem contato com o servidor)")

            leit = linha_dict(ultima_leitura(con))
            motivos, prot_ate = protecao(con, cfg, agora)
            fila = [dict(p) for p in fila_ativa(con)]
            for i, p in enumerate(fila):
                p["posicao"] = i + 1
                p["descricao"] = descrever_pedido(p)

            if desde:
                evs = con.execute("SELECT * FROM eventos WHERE id>? ORDER BY id LIMIT 50", (desde,)).fetchall()
            else:
                evs = list(reversed(con.execute("SELECT * FROM eventos ORDER BY id DESC LIMIT 30").fetchall()))
            ultimo_ev = con.execute("SELECT MAX(id) FROM eventos").fetchone()[0] or 0

            h0 = hoje_inicio()
            st = con.execute("""SELECT COUNT(*) n, MIN(temp) tmin, MAX(temp) tmax, AVG(temp) tavg,
                                MIN(solo) smin, MAX(solo) smax FROM leituras WHERE em>=?""", (h0,)).fetchone()
            regas_hoje = con.execute("SELECT * FROM regas WHERE inicio>=? OR fim IS NULL", (h0,)).fetchall()
            seg_bomba = sum(((r["fim"] or agora) - max(r["inicio"], h0)) for r in regas_hoje)
            auto_hoje = [r for r in regas_hoje if r["motivo"] == 1]
            seg_auto = sum(((r["fim"] or agora) - max(r["inicio"], h0)) for r in auto_hoje)
            n_com = \
            con.execute("SELECT COUNT(*) FROM comunicacao WHERE em>=? AND direcao='recebido'", (h0,)).fetchone()[0]
            regas_rec = regas_com_config(con.execute("SELECT * FROM regas ORDER BY id DESC LIMIT 10"), agora)
            ult_auto = con.execute("SELECT * FROM regas WHERE motivo=1 ORDER BY id DESC LIMIT 1").fetchone()
            pres = [dict(r) for r in con.execute("SELECT nome,visto FROM presenca WHERE visto>? ORDER BY nome",
                                                 (agora - PRESENCA_S,))]
            lock = lock_cfg(con)
            ocup = quem_ocupa(con)
            n_total = con.execute("SELECT COUNT(*) FROM leituras").fetchone()[0]
            mais_antiga = con.execute("SELECT MIN(em) FROM leituras").fetchone()[0]

        try:
            tam_db = os.path.getsize(DB)
        except OSError:
            tam_db = 0
        return jsonify(
            agora=agora, online=online, dispositivo=disp, leitura=leit, config=cfg,
            config_aplicada=bool(disp) and disp.get("cv") == cfg["versao"],
            lock=lock, ocupado=ocup, fila=fila,
            protecao={"ativa": bool(motivos), "motivos": motivos, "ate": prot_ate},
            eventos=[dict(e) for e in evs], ultimo_evento=ultimo_ev, inicial=not desde,
            hoje={"leituras": st["n"], "tmin": st["tmin"], "tmax": st["tmax"], "tavg": st["tavg"],
                  "smin": st["smin"], "smax": st["smax"], "regas": len(regas_hoje),
                  "bomba_s": int(seg_bomba), "comunicacoes": n_com,
                  "regas_auto": len(auto_hoje), "auto_s": int(seg_auto)},
            regas=regas_rec, ultima_auto=regas_com_config([ult_auto], agora)[0] if ult_auto else None,
            presenca=pres, hw=avaliar_hw(disp) if disp else avaliar_hw(None),
            hw_limites={"ram_total": RAM_TOTAL, "flash_total": FLASH_TOTAL},
            banco={"tamanho": tam_db, "leituras": n_total, "desde": mais_antiga, "retencao_dias": RETENCAO_S // 86400},
            textos={"modos": MODOS, "motivos": MOTIVOS, "leds": LEDS, "falhas": FALHAS, "erros": ERROS_SENSOR},
            padroes=CFG_PADRAO, limites=LIMITES, duracao_recomendada=DURACAO_RECOMENDADA,
            senha_padrao=(SENHA == "fatec2026"),
        )
    finally:
        con.close()


@app.route("/api/serie")
def api_serie():
    """Leituras de uma janela (minutos), agrupadas em no maximo 240 pontos."""
    try:
        minutos = max(5, min(int(request.args.get("min", 60)), RETENCAO_S // 60))
    except ValueError:
        minutos = 60
    agora = time.time()
    ini = agora - minutos * 60
    con = db()
    try:
        rows = con.execute("SELECT em,solo,temp,ldr,bomba,ram,carga,lmx,vcc FROM leituras WHERE em>=? ORDER BY em",
                           (ini,)).fetchall()
    finally:
        con.close()
    n_max = 240
    passo = (minutos * 60) / n_max
    grupos = {}
    for r in rows:
        k = int((r["em"] - ini) // passo)
        grupos.setdefault(k, []).append(r)
    pontos = []
    for k in sorted(grupos):
        g = grupos[k]
        p = {"em": sum(x["em"] for x in g) / len(g)}
        for campo in ("solo", "temp", "ldr", "ram", "carga", "lmx", "vcc"):
            vals = [x[campo] for x in g if x[campo] is not None]
            p[campo] = round(sum(vals) / len(vals), 1) if vals else None
        p["bomba"] = max((x["bomba"] or 0) for x in g)
        pontos.append(p)
    return jsonify(inicio=ini, fim=agora, minutos=minutos, pontos=pontos, brutos=len(rows))


@app.route("/api/regar", methods=["POST"])
def api_regar():
    b = corpo()
    nome = nome_valido(b.get("nome"))
    if not nome:
        return erro("Informe seu nome")
    agora = time.time()
    con = db()
    try:
        with con:
            con.execute("BEGIN IMMEDIATE")
            cfg = get_cfg(con)
            if cfg["modo"] == 2:
                return erro("O regador está em modo DESLIGADO. Ligue em Ajustes para pedir rega.", 409)
            try:
                dur = int(b.get("duracao") or cfg["duracao"])
            except (TypeError, ValueError):
                return erro("Duração inválida")
            if not 2 <= dur <= 600:
                return erro("A duração deve ficar entre 2 e 600 segundos")
            motivos, _ = protecao(con, cfg, agora)
            senha = b.get("senha")
            if motivos and not senha_ok(senha):
                return erro("Senha incorreta" if senha else "Proteção contra encharcar ativa",
                            403, precisa_senha=True, motivos=motivos)
            ocup = quem_ocupa(con)
            cur = con.execute("INSERT INTO pedidos(criado,tipo,duracao,quem,prioridade,status,forcado,obs,vel) "
                              "VALUES(?,?,?,?,?,?,?,?,?)",
                              (agora, "regar", dur, nome, 0, "fila", 1 if motivos else 0,
                               "; ".join(motivos) if motivos else None, cfg["velm"]))
            pos = len(fila_ativa(con))
            msg = f"{nome} pediu uma rega de {dur} s · potência {txt_vel(cfg['velm'])} (posição {pos} na fila)"
            if dur > DURACAO_RECOMENDADA:
                msg += f" — acima dos {DURACAO_RECOMENDADA} s recomendados"
            if motivos:
                msg += " — liberou a proteção com senha: " + "; ".join(motivos)
            evento(con, "pedido", msg, nome)
        aviso = None
        if ocup:
            aviso = (f"{ocup['quem']} já está enviando dados ao regador automático ({ocup['o_que']}). "
                     f"Seu pedido entrou na fila na posição {pos}.")
        return jsonify(ok=True, id=cur.lastrowid, posicao=pos, aviso=aviso)
    finally:
        con.close()


def pedido_simples(tipo, texto_evento):
    b = corpo()
    nome = nome_valido(b.get("nome"))
    if not nome:
        return erro("Informe seu nome")
    con = db()
    try:
        with con:
            con.execute("BEGIN IMMEDIATE")
            ja = con.execute("SELECT id FROM pedidos WHERE tipo=? AND status='fila'", (tipo,)).fetchone()
            if ja:
                return jsonify(ok=True, aviso="Esse comando já está na fila; não foi duplicado.")
            ocup = quem_ocupa(con)
            cur = con.execute("INSERT INTO pedidos(criado,tipo,duracao,quem,prioridade,status) VALUES(?,?,?,?,?,?)",
                              (time.time(), tipo, 0, nome, 1, "fila"))
            evento(con, "pedido", f"{nome} {texto_evento}", nome)
        aviso = None
        if ocup:
            aviso = f"{ocup['quem']} já está enviando dados ao regador. Seu comando tem prioridade e vai na frente da fila."
        return jsonify(ok=True, id=cur.lastrowid, aviso=aviso)
    finally:
        con.close()


@app.route("/api/parar", methods=["POST"])
def api_parar():
    return pedido_simples("parar", "pediu para PARAR a bomba (o automático pausa 5 min)")


@app.route("/api/retomar", methods=["POST"])
def api_retomar():
    return pedido_simples("retomar", "pediu para RETOMAR o modo automático")


@app.route("/api/cancelar", methods=["POST"])
def api_cancelar():
    b = corpo()
    nome = nome_valido(b.get("nome"))
    con = db()
    try:
        with con:
            p = con.execute("SELECT * FROM pedidos WHERE id=?", (b.get("id"),)).fetchone()
            if not p or p["status"] != "fila":
                return erro("Esse pedido não está mais na fila (já foi entregue ao Arduino ou cancelado)", 409)
            if p["quem"] != nome and not senha_ok(b.get("senha")):
                return erro("Só quem pediu pode cancelar sem senha", 403, precisa_senha=True,
                            motivos=[f"Pedido feito por {p['quem']}"])
            con.execute("UPDATE pedidos SET status='cancelado' WHERE id=?", (p["id"],))
            evento(con, "pedido", f"{nome or '?'} cancelou: {descrever_pedido(p)} (pedido de {p['quem']})", nome)
        return jsonify(ok=True)
    finally:
        con.close()


@app.route("/api/fila/limpar", methods=["POST"])
def api_limpar_fila():
    b = corpo()
    nome = nome_valido(b.get("nome"))
    if not senha_ok(b.get("senha")):
        return erro("Senha incorreta", 403, precisa_senha=True, motivos=["Limpar a fila exige senha"])
    con = db()
    try:
        with con:
            n = con.execute("UPDATE pedidos SET status='cancelado' WHERE status='fila'").rowcount
            evento(con, "pedido", f"{nome or '?'} limpou a fila ({n} pedido(s) cancelado(s))", nome)
        return jsonify(ok=True, cancelados=n)
    finally:
        con.close()


def fmt_valor(k, v):
    if k == "modo":
        return MODOS[v]
    if k in ("vel", "velm"):
        return txt_vel(v)
    return str(v)


def salvar_config(nome, novo, origem):
    con = db()
    try:
        with con:
            con.execute("BEGIN IMMEDIATE")
            atual = get_cfg(con)
            cfg, msg = validar_cfg(atual, novo)
            if msg:
                return erro(msg)
            muda_arduino = any(cfg[k] != atual[k] for k in CAMPOS_ARDUINO)
            if muda_arduino:
                lock = lock_cfg(con)
                if lock and not lock["expirado"]:
                    return erro(f"{lock['quem']} já está enviando uma configuração ao regador automático "
                                f"(v{lock['versao']}). Aguarde o Arduino confirmar e tente de novo.", 409, lock=lock)
            mudou = [f"{NOMES_CAMPOS[k]}: {fmt_valor(k, atual[k])} → {fmt_valor(k, cfg[k])}"
                     for k in CFG_PADRAO if cfg[k] != atual[k]]
            if not mudou:
                return jsonify(ok=True, aviso="Nada mudou.")
            kv_set(con, "config", cfg)
            if muda_arduino:
                versao = atual["versao"] + 1
                kv_set(con, "cfg_versao", versao)
                kv_set(con, "lock_cfg", {"quem": nome, "desde": time.time(), "versao": versao})
            evento(con, "config", f"{nome} {origem}: " + ", ".join(mudou), nome)
        return jsonify(ok=True, aguardando_arduino=muda_arduino)
    finally:
        con.close()


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        con = db()
        try:
            return jsonify(get_cfg(con))
        finally:
            con.close()
    b = corpo()
    nome = nome_valido(b.get("nome"))
    if not nome:
        return erro("Informe seu nome")
    if not senha_ok(b.get("senha")):
        return erro("Senha incorreta" if b.get("senha") else "Mudar a configuração exige senha", 403,
                    precisa_senha=True, motivos=["Mudar a configuração exige senha"])
    return salvar_config(nome, b, b.get("origem") or "alterou a configuração")


@app.route("/api/padroes", methods=["POST"])
def api_padroes():
    b = corpo()
    nome = nome_valido(b.get("nome")) or "?"
    if not senha_ok(b.get("senha")):
        return erro("Senha incorreta", 403, precisa_senha=True, motivos=["Restaurar padrões exige senha"])
    return salvar_config(nome, dict(CFG_PADRAO), "restaurou os valores padrão")


@app.route("/api/historico")
def api_historico():
    tipo = request.args.get("tipo", "eventos")
    try:
        n = max(1, min(int(request.args.get("n", 150)), 1000))
    except ValueError:
        n = 150
    direcao = request.args.get("direcao")
    if tipo not in ("eventos", "comunicacao", "pedidos", "leituras", "regas"):
        return erro("tipo inválido")
    con = db()
    try:
        sql, args = f"SELECT * FROM {tipo}", []
        if tipo == "comunicacao" and direcao in ("recebido", "enviado"):
            sql += " WHERE direcao=?"
            args.append(direcao)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(n)
        rows = [dict(r) for r in con.execute(sql, args)]
        if tipo == "regas":
            rows = regas_com_config(rows, time.time())
        return jsonify(rows)
    finally:
        con.close()


@app.route("/api/exportar.csv")
def api_csv():
    tipo = request.args.get("tipo", "leituras")
    if tipo not in ("leituras", "eventos", "comunicacao", "pedidos", "regas"):
        return erro("tipo inválido")
    con = db()
    try:
        rows = con.execute(f"SELECT * FROM {tipo} ORDER BY id DESC").fetchall()
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        if rows:
            w.writerow(["data_hora"] + list(rows[0].keys()))
            for r in rows:
                ts = r["em"] if "em" in r.keys() else (r["criado"] if "criado" in r.keys() else r["inicio"])
                w.writerow([datetime.fromtimestamp(ts, TZ).strftime("%d/%m/%Y %H:%M:%S") if ts else ""] + list(r))
        return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=regador_{tipo}.csv"})
    finally:
        con.close()


@app.route("/")
def painel():
    resp = send_from_directory(BASE, "painel.html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp

@app.errorhandler(Exception)
def falha_geral(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    app.logger.exception("erro")
    if request.path.startswith("/api/"):
        return erro(f"Erro interno: {type(e).__name__}", 500)
    return Response("Erro interno", 500)

init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)