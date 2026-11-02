# =====================================================================
#  Regador Inteligente - servidor (PythonAnywhere, HTTP porta 80)
# =====================================================================
#  O Arduino (ESP-01 com firmware AT) chama GET /d a cada ciclo.
#  A resposta e UMA linha de texto com a configuracao e, se houver,
#  um comando da fila:
#     CFG;i=10;lo=34;hi=65;m=0;v=-1;cv=3;c=1;cid=17;cd=20;FIM
#
#  Rede de usuarios (painel web):
#   - todos veem tudo e todos podem pedir rega / parar
#   - pedidos de rega entram numa FILA (buffer) e sao entregues um por vez
#   - toda acao vira um EVENTO que aparece para todos os conectados
#   - mudar configuracao trava ate o Arduino confirmar (outro usuario
#     recebe o aviso "fulano ja esta enviando dados ao regador")
#   - protecao contra encharcar: rega recente, solo umido ou rega ja na
#     fila -> so prossegue com senha
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
DB = os.path.join(BASE, "regador.db")  # banco novo (o irrigacao.db antigo pode ser apagado)

TOKEN = os.environ.get("DEVICE_TOKEN", "irriga123")  # igual ao TOKEN do Arduino
SENHA = os.environ.get("SENHA_PAINEL", "fatec2026")  # senha das acoes de risco

# Padroes = valores do seu codigo original
CFG_PADRAO = {"intervalo": 10, "lo": 34, "hi": 65, "modo": 0, "vel": -1, "duracao": 20, "protecao": 30}
LIMITES = {"intervalo": (5, 600), "lo": (0, 99), "hi": (1, 100), "modo": (0, 2),
           "vel": (-1, 255), "duracao": (1, 600), "protecao": (0, 1440)}
CAMPOS_ARDUINO = ("intervalo", "lo", "hi", "modo", "vel")  # os que o Arduino precisa confirmar

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

LOCK_CFG_S = 120  # trava de configuracao expira se o Arduino nao confirmar
PRESENCA_S = 15  # considerado "conectado" se consultou nos ultimos 15 s
MAX_LEITURAS = 20000
MAX_COMUNICACAO = 600
MAX_EVENTOS = 1500
MAX_PEDIDOS = 1500

app = Flask(__name__)


# ---------------------------------------------------------------------
#  Banco
# ---------------------------------------------------------------------
def db():
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS leituras(id INTEGER PRIMARY KEY, em REAL, origem TEXT, temp REAL,
            ldr INT, solo_raw INT, solo INT, pot INT, vel INT, bomba INT, motivo INT, led INT,
            modo INT, erros INT);
        CREATE INDEX IF NOT EXISTS ix_leituras_em ON leituras(em);
        CREATE TABLE IF NOT EXISTS eventos(id INTEGER PRIMARY KEY, em REAL, tipo TEXT, quem TEXT, msg TEXT);
        CREATE TABLE IF NOT EXISTS comunicacao(id INTEGER PRIMARY KEY, em REAL, direcao TEXT, resumo TEXT);
        CREATE TABLE IF NOT EXISTS pedidos(id INTEGER PRIMARY KEY, criado REAL, tipo TEXT, duracao INT,
            quem TEXT, prioridade INT, status TEXT, forcado INT DEFAULT 0, obs TEXT,
            entregue REAL, executado REAL);
        CREATE TABLE IF NOT EXISTS regas(id INTEGER PRIMARY KEY, inicio REAL, fim REAL, motivo INT,
            quem TEXT, origem TEXT);
        CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS presenca(nome TEXT PRIMARY KEY, visto REAL);
        """)


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


def aparar(con):
    """Mantem as tabelas num tamanho saudavel (corta pelo id, barato)."""
    for tabela, maximo in (("leituras", MAX_LEITURAS), ("comunicacao", MAX_COMUNICACAO),
                           ("eventos", MAX_EVENTOS), ("pedidos", MAX_PEDIDOS)):
        con.execute(f"DELETE FROM {tabela} WHERE id <= (SELECT MAX(id) FROM {tabela}) - ?", (maximo,))


def get_cfg(con):
    cfg = dict(CFG_PADRAO)
    cfg.update(kv_get(con, "config", {}))
    cfg["versao"] = kv_get(con, "cfg_versao", 1)
    return cfg


def validar_cfg(base, novo):
    cfg = {k: base[k] for k in CFG_PADRAO}
    for chave, (mn, mx) in LIMITES.items():
        if chave in novo and str(novo[chave]).strip() != "":
            try:
                val = int(float(novo[chave]))
            except (TypeError, ValueError):
                return None, f"'{chave}' inválido"
            if not mn <= val <= mx:
                return None, f"'{chave}' deve ficar entre {mn} e {mx}"
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


# ---------------------------------------------------------------------
#  Regras de negocio
# ---------------------------------------------------------------------
def fila_ativa(con):
    """Pedidos ainda nao executados, na ordem em que serao entregues."""
    return con.execute("""SELECT * FROM pedidos WHERE status IN ('entregue','fila')
                          ORDER BY CASE status WHEN 'entregue' THEN 0 ELSE 1 END, prioridade DESC, id""").fetchall()


def ultima_leitura(con):
    return con.execute("SELECT * FROM leituras WHERE origem='ao vivo' ORDER BY id DESC LIMIT 1").fetchone()


def protecao(con, cfg, agora):
    """Motivos que exigem senha para uma nova rega (protecao contra encharcar)."""
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
    if leit and leit["solo"] >= cfg["hi"]:
        motivos.append(f"O solo já está úmido ({leit['solo']}% >= {cfg['hi']}%)")
    n = con.execute("SELECT COUNT(*) FROM pedidos WHERE tipo='regar' AND status IN ('fila','entregue')").fetchone()[0]
    if n:
        motivos.append(f"Já existe{'m' if n > 1 else ''} {n} rega{'s' if n > 1 else ''} na fila")
    return motivos, ate


def descrever_pedido(p):
    if p["tipo"] == "regar":
        return f"rega de {p['duracao']} s"
    return {"parar": "parar a bomba", "retomar": "retomar o automático"}.get(p["tipo"], p["tipo"])


def quem_ocupa(con):
    """Quem esta 'enviando dados ao regador' agora (para o aviso)."""
    lock = lock_cfg(con)
    if lock and not lock["expirado"]:
        return {"quem": lock["quem"], "o_que": f"uma configuração (v{lock['versao']})"}
    fila = fila_ativa(con)
    if fila:
        return {"quem": fila[0]["quem"], "o_que": descrever_pedido(fila[0])}
    return None


def lock_cfg(con):
    lock = kv_get(con, "lock_cfg")
    if not lock:
        return None
    lock["expirado"] = time.time() - lock["desde"] > LOCK_CFG_S
    return lock


def registrar_bomba(con, em, ligada, motivo, origem):
    """Cria/fecha registros de rega a partir das transicoes da bomba."""
    antes = kv_get(con, "bomba_estado", 0)
    if ligada and not antes:
        quem = None
        if motivo == 2:
            p = con.execute("SELECT quem FROM pedidos WHERE tipo='regar' AND status IN ('entregue','executado') "
                            "ORDER BY id DESC LIMIT 1").fetchone()
            quem = p["quem"] if p else None
        con.execute("INSERT INTO regas(inicio,motivo,quem,origem) VALUES(?,?,?,?)", (em, motivo, quem, origem))
        txt = MOTIVOS.get(motivo, "?")
        evento(con, "rega", f"Bomba LIGOU - rega {txt}" + (f" pedida por {quem}" if quem else "")
               + (" (registrada enquanto estava offline)" if origem == "buffer" else ""), quem, em)
    elif not ligada and antes:
        r = con.execute("SELECT * FROM regas WHERE fim IS NULL ORDER BY id DESC LIMIT 1").fetchone()
        dur = ""
        if r:
            con.execute("UPDATE regas SET fim=? WHERE id=?", (em, r["id"]))
            dur = f" após {int(em - r['inicio'])} s"
        evento(con, "rega", f"Bomba DESLIGOU{dur}", None, em)
    kv_set(con, "bomba_estado", 1 if ligada else 0)


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
            for h in hist:
                con.execute("INSERT INTO leituras(em,origem,temp,ldr,solo,bomba,erros) VALUES(?,?,?,?,?,?,?)",
                            (h["em"], "buffer", h["temp"], h["ldr"], h["solo"], h["bomba"], h["erros"]))
                registrar_bomba(con, h["em"], h["bomba"], -1, "buffer")

            # ---- leitura atual (todos os sensores) ----
            cur = {"temp": gi("t") / 10, "ldr": gi("l"), "solo_raw": gi("sr"), "solo": gi("s"),
                   "pot": gi("p"), "vel": gi("v"), "bomba": gi("b"), "motivo": gi("bm"),
                   "led": gi("led"), "modo": gi("m"), "erros": gi("e")}
            con.execute("""INSERT INTO leituras(em,origem,temp,ldr,solo_raw,solo,pot,vel,bomba,motivo,led,modo,erros)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (agora, "ao vivo", cur["temp"], cur["ldr"], cur["solo_raw"], cur["solo"], cur["pot"],
                         cur["vel"], cur["bomba"], cur["motivo"], cur["led"], cur["modo"], cur["erros"]))
            registrar_bomba(con, agora, cur["bomba"], cur["motivo"], "ao vivo")

            # ---- estado interno do Arduino ----
            disp = {"visto": agora, "up": gi("up"), "falha": gi("f"), "nfalhas": gi("n"), "buffer": gi("q"),
                    "descartadas": gi("x"), "regas": gi("rg"), "bomba_s": gi("lig"), "ram": gi("ram"),
                    "atrasos": gi("atr"), "cv": gi("cv"), "ack": gi("a"), "restante": gi("rr"),
                    "pausa": gi("pz"), "hist_recebido": len(hist),
                    "ip": request.headers.get("X-Real-IP", request.remote_addr)}
            if visto_antes and agora - visto_antes > online_ate(cfg):
                evento(con, "online", "Regador voltou a ficar ONLINE")
            elif not visto_antes:
                evento(con, "online", "Regador conectou pela primeira vez")
            if disp_antes and disp["up"] + 5 < disp_antes.get("up", 0):
                evento(con, "alerta", "O Arduino reiniciou (o contador de tempo ligado zerou)")
            if disp["falha"]:
                evento(con, "alerta", f"Arduino voltou após {disp['nfalhas']} falha(s). Ultima: "
                                      f"{FALHAS.get(disp['falha'], disp['falha'])}. Enviou {len(hist)} leitura(s) guardada(s).")
            if cur["erros"] != disp_antes.get("erros", 0):
                if cur["erros"]:
                    evento(con, "alerta", "Sensor com problema: " +
                           "; ".join(t for b, t in ERROS_SENSOR.items() if cur["erros"] & b))
                else:
                    evento(con, "info", "Sensores normalizados")
            disp["erros"] = cur["erros"]
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
                        continue  # espera a rega manual atual terminar / modo desligado segura a fila
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
                     f"cv={cfg['versao']};c={c};cid={cid};cd={cd};FIM\n")

            comunicacao(con, "recebido",
                        f"T={cur['temp']:.1f}C solo={cur['solo']}% (raw {cur['solo_raw']}) LDR={cur['ldr']} "
                        f"pot={cur['pot']} bomba={'ON' if cur['bomba'] else 'off'} vel={cur['vel']} "
                        f"| +{len(hist)} do buffer | buffer Arduino={disp['buffer']} | ack={disp['ack']} cv={disp['cv']}")
            comunicacao(con, "enviado", linha.strip())
            aparar(con)
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
            n_com = \
            con.execute("SELECT COUNT(*) FROM comunicacao WHERE em>=? AND direcao='recebido'", (h0,)).fetchone()[0]
            serie = [dict(r) for r in reversed(con.execute(
                "SELECT em,solo,temp,ldr,bomba FROM leituras ORDER BY em DESC LIMIT 120").fetchall())]
            regas_rec = [dict(r) for r in con.execute("SELECT * FROM regas ORDER BY id DESC LIMIT 8")]
            pres = [dict(r) for r in con.execute("SELECT nome,visto FROM presenca WHERE visto>? ORDER BY nome",
                                                 (agora - PRESENCA_S,))]
            lock = lock_cfg(con)
            ocup = quem_ocupa(con)

        return jsonify(
            agora=agora, online=online, dispositivo=disp, leitura=leit, config=cfg,
            config_aplicada=bool(disp) and disp.get("cv") == cfg["versao"],
            lock=lock, ocupado=ocup, fila=fila,
            protecao={"ativa": bool(motivos), "motivos": motivos, "ate": prot_ate},
            eventos=[dict(e) for e in evs], ultimo_evento=ultimo_ev, inicial=not desde,
            hoje={"leituras": st["n"], "tmin": st["tmin"], "tmax": st["tmax"], "tavg": st["tavg"],
                  "smin": st["smin"], "smax": st["smax"], "regas": len(regas_hoje),
                  "bomba_s": int(seg_bomba), "comunicacoes": n_com},
            serie=serie, regas=regas_rec, presenca=pres,
            textos={"modos": MODOS, "motivos": MOTIVOS, "leds": LEDS, "falhas": FALHAS, "erros": ERROS_SENSOR},
            senha_padrao=(SENHA == "fatec2026"),
        )
    finally:
        con.close()


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
                return erro("O regador está em modo DESLIGADO. Mude o modo em Ajustes para pedir rega.", 409)
            try:
                dur = int(b.get("duracao") or cfg["duracao"])
            except (TypeError, ValueError):
                return erro("Duração inválida")
            if not 1 <= dur <= 600:
                return erro("A duração deve ficar entre 1 e 600 segundos")
            motivos, _ = protecao(con, cfg, agora)
            senha = b.get("senha")
            if motivos and not senha_ok(senha):
                return erro("Senha incorreta" if senha else "Proteção contra encharcar ativa",
                            403, precisa_senha=True, motivos=motivos)
            ocup = quem_ocupa(con)
            cur = con.execute("INSERT INTO pedidos(criado,tipo,duracao,quem,prioridade,status,forcado,obs) "
                              "VALUES(?,?,?,?,?,?,?,?)",
                              (agora, "regar", dur, nome, 0, "fila", 1 if motivos else 0,
                               "; ".join(motivos) if motivos else None))
            pos = len(fila_ativa(con))
            msg = f"{nome} pediu uma rega de {dur} s (posição {pos} na fila)"
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
            kv_set(con, "config", cfg)
            mudou = [f"{k}: {atual[k]} -> {cfg[k]}" for k in CFG_PADRAO if cfg[k] != atual[k]]
            if not mudou:
                return jsonify(ok=True, aviso="Nada mudou.")
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
        return erro("Senha incorreta", 403, precisa_senha=True, motivos=["Mudar a configuração exige senha"])
    return salvar_config(nome, b, "alterou a configuração")


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
    n = max(1, min(int(request.args.get("n", 100) or 100), 1000))
    direcao = request.args.get("direcao")
    tabelas = {"eventos": "eventos", "comunicacao": "comunicacao", "pedidos": "pedidos",
               "leituras": "leituras", "regas": "regas"}
    if tipo not in tabelas:
        return erro("tipo invalido")
    con = db()
    try:
        sql, args = f"SELECT * FROM {tabelas[tipo]}", []
        if tipo == "comunicacao" and direcao in ("recebido", "enviado"):
            sql += " WHERE direcao=?"
            args.append(direcao)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(n)
        return jsonify([dict(r) for r in con.execute(sql, args)])
    finally:
        con.close()


@app.route("/api/exportar.csv")
def api_csv():
    tipo = request.args.get("tipo", "leituras")
    if tipo not in ("leituras", "eventos", "comunicacao", "pedidos", "regas"):
        return erro("tipo invalido")
    con = db()
    try:
        rows = con.execute(f"SELECT * FROM {tipo} ORDER BY id DESC LIMIT 20000").fetchall()
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