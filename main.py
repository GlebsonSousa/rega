# Servidor da Irrigacao Inteligente - hospedar no PythonAnywhere (plano gratuito)
# Aceita HTTP puro (porta 80), que e o que o ESP-01 com firmware AT consegue falar.
#
# ROTAS
#   Dispositivo (Arduino/ESP-01):
#     GET  /d?k=TOKEN&t=25.1&l=700&s=40&b=0&v=200&e=0&f=0&n=0
#          -> grava a leitura e responde UMA linha com a configuracao:
#             CFG;i=10;r=5;s=34;u=65;m=0;v=-1;c=0;FIM
#   Painel (navegador):
#     GET  /                 painel com leituras, erros e formulario de configuracao
#     POST /config           salva configuracao (formulario, pede senha)
#     POST /cmd              comando unico: regar / parar (formulario, pede senha)
#   API (JSON, para app/outro servidor):
#     GET  /api/estado       ultima leitura, online/offline, config, eventos
#     GET  /api/leituras?n=50
#     GET  /api/config
#     POST /api/config       JSON {"tempoRega":8,...}  header X-Senha
#     POST /api/cmd          JSON {"acao":"regar"}     header X-Senha

import os, json, time, sqlite3
from flask import Flask, request, Response, jsonify, redirect, render_template_string

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "irrigacao.db")

TOKEN = os.environ.get("DEVICE_TOKEN", "irriga123")       # igual ao TOKEN do Arduino
SENHA_PAINEL = os.environ.get("SENHA_PAINEL", "fatec2026")  # senha para mudar config

CFG_PADRAO = {"intervalo": 10, "tempoRega": 5, "limSeco": 34, "limUmido": 65, "modo": 0, "vel": -1}
MODOS = {0: "automatico", 1: "manual", 2: "desligado"}
CMDS = {"nada": 0, "regar": 1, "parar": 2}

# Codigos que o Arduino envia (iguais aos do sketch)
FALHAS = {
    0: "ok", 1: "ESP nao responde ao AT", 2: "falha ao conectar no Wi-Fi",
    3: "falha ao abrir TCP (CIPSTART/DNS)", 4: "falha no CIPSEND", 5: "envio nao confirmado (SEND OK)",
    6: "servidor nao respondeu a tempo", 7: "servidor recusou (token/dados)", 8: "resposta incompleta/invalida",
    9: "requisicao grande demais",
}
ERROS_SENSOR = {1: "temperatura fora da faixa (TMP36)", 2: "sensor de solo instavel/desconectado",
                4: "LDR com leitura instavel"}

app = Flask(__name__)


# ---------------- banco ----------------
def db():
    con = sqlite3.connect(DB, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS leituras(
            id INTEGER PRIMARY KEY, em REAL, temp REAL, luz INT, solo INT, bomba INT,
            vel INT, erros INT, falha INT, nfalhas INT)""")
        con.execute("CREATE TABLE IF NOT EXISTS eventos(id INTEGER PRIMARY KEY, em REAL, msg TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")


def kv_get(k, padrao):
    with db() as con:
        r = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return json.loads(r["v"]) if r else padrao


def kv_set(k, v):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, json.dumps(v)))


def evento(msg):
    with db() as con:
        con.execute("INSERT INTO eventos(em,msg) VALUES(?,?)", (time.time(), msg))
        con.execute("DELETE FROM eventos WHERE id NOT IN (SELECT id FROM eventos ORDER BY id DESC LIMIT 200)")


def get_cfg():
    cfg = dict(CFG_PADRAO)
    cfg.update(kv_get("config", {}))
    return cfg


def validar_cfg(novo):
    """Recebe dict (strings ou numeros), devolve (cfg, erro)."""
    cfg = get_cfg()
    limites = {"intervalo": (5, 3600), "tempoRega": (1, 600), "limSeco": (0, 100),
               "limUmido": (0, 100), "modo": (0, 2), "vel": (-1, 255)}
    for chave, (mn, mx) in limites.items():
        if chave in novo and str(novo[chave]).strip() != "":
            try:
                val = int(float(novo[chave]))
            except ValueError:
                return None, f"{chave} invalido"
            if not mn <= val <= mx:
                return None, f"{chave} deve ficar entre {mn} e {mx}"
            cfg[chave] = val
    if cfg["limSeco"] >= cfg["limUmido"]:
        return None, "limSeco precisa ser menor que limUmido"
    return cfg, None


def online(cfg):
    ultimo = kv_get("ultimo_contato", 0)
    return (time.time() - ultimo) < max(3 * cfg["intervalo"], 30), ultimo


def descrever_erros(e):
    return [txt for bit, txt in ERROS_SENSOR.items() if e & bit]


# ---------------- rota do dispositivo ----------------
def texto(s, status=200):
    # resposta curtinha, sem chunked: o Uno le facil pela serial
    return Response(s, status=status, mimetype="text/plain")


@app.route("/d")
def dispositivo():
    q = request.args
    if q.get("k") != TOKEN:
        return texto("ERR;token\n", 401)
    try:
        temp = float(q.get("t", "nan"))
        luz, solo = int(q.get("l", -1)), int(q.get("s", -1))
        bomba, vel = int(q.get("b", 0)), int(q.get("v", 0))
        erros, falha, nfalhas = int(q.get("e", 0)), int(q.get("f", 0)), int(q.get("n", 0))
    except ValueError:
        return texto("ERR;dados\n", 400)

    agora = time.time()
    with db() as con:
        con.execute("INSERT INTO leituras(em,temp,luz,solo,bomba,vel,erros,falha,nfalhas) VALUES(?,?,?,?,?,?,?,?,?)",
                    (agora, temp, luz, solo, bomba, vel, erros, falha, nfalhas))
        con.execute("DELETE FROM leituras WHERE id NOT IN (SELECT id FROM leituras ORDER BY id DESC LIMIT 5000)")

    # Registra problemas relatados pelo Arduino
    if falha:
        evento(f"Arduino voltou apos {nfalhas} falha(s). Ultima: {FALHAS.get(falha, falha)}")
    erros_antes = kv_get("ultimos_erros", 0)
    if erros != erros_antes:
        if erros:
            evento("Sensor: " + "; ".join(descrever_erros(erros)))
        else:
            evento("Sensores normalizados")
        kv_set("ultimos_erros", erros)
    if not online(get_cfg())[0]:
        evento("Dispositivo ficou online")
    kv_set("ultimo_contato", agora)

    cfg = get_cfg()
    cmd = kv_get("cmd", "nada")
    kv_set("cmd", "nada")  # comando e de uso unico
    linha = (f"CFG;i={cfg['intervalo']};r={cfg['tempoRega']};s={cfg['limSeco']};u={cfg['limUmido']};"
             f"m={cfg['modo']};v={cfg['vel']};c={CMDS.get(cmd, 0)};FIM\n")
    return texto(linha)


# ---------------- API JSON ----------------
def ultimas(n):
    with db() as con:
        rows = con.execute("SELECT * FROM leituras ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    return [dict(r) for r in rows]


def senha_ok(s):
    return s == SENHA_PAINEL


@app.route("/api/estado")
def api_estado():
    cfg = get_cfg()
    esta_online, ultimo = online(cfg)
    lst = ultimas(1)
    ult = lst[0] if lst else None
    if ult:
        ult["erros_txt"] = descrever_erros(ult["erros"])
    with db() as con:
        evs = [dict(r) for r in con.execute("SELECT em,msg FROM eventos ORDER BY id DESC LIMIT 10")]
    return jsonify(online=esta_online, ultimo_contato=ultimo, ultima=ult, config=cfg,
                   modo_txt=MODOS[cfg["modo"]], cmd_pendente=kv_get("cmd", "nada"), eventos=evs)


@app.route("/api/leituras")
def api_leituras():
    n = min(int(request.args.get("n", 50)), 1000)
    return jsonify(ultimas(n))


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        return jsonify(get_cfg())
    if not senha_ok(request.headers.get("X-Senha")):
        return jsonify(erro="senha invalida"), 401
    cfg, erro = validar_cfg(request.get_json(silent=True) or {})
    if erro:
        return jsonify(erro=erro), 400
    kv_set("config", cfg)
    evento(f"Configuracao alterada via API: {cfg}")
    return jsonify(ok=True, config=cfg)


@app.route("/api/cmd", methods=["POST"])
def api_cmd():
    if not senha_ok(request.headers.get("X-Senha")):
        return jsonify(erro="senha invalida"), 401
    acao = (request.get_json(silent=True) or {}).get("acao", "")
    if acao not in CMDS:
        return jsonify(erro="acao deve ser regar, parar ou nada"), 400
    kv_set("cmd", acao)
    return jsonify(ok=True, cmd=acao)


# ---------------- painel ----------------
@app.route("/config", methods=["POST"])
def form_config():
    if not senha_ok(request.form.get("senha")):
        return redirect("/?msg=Senha+incorreta")
    cfg, erro = validar_cfg(request.form)
    if erro:
        return redirect("/?msg=" + erro.replace(" ", "+"))
    kv_set("config", cfg)
    evento(f"Configuracao alterada pelo painel: {cfg}")
    return redirect("/?msg=Configuracao+salva.+O+Arduino+aplica+no+proximo+envio")


@app.route("/cmd", methods=["POST"])
def form_cmd():
    if not senha_ok(request.form.get("senha")):
        return redirect("/?msg=Senha+incorreta")
    acao = request.form.get("acao", "nada")
    if acao in CMDS:
        kv_set("cmd", acao)
    return redirect("/?msg=Comando+" + acao + "+enviado")


PAINEL = """<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Irrigacao Inteligente</title>
<style>
:root{--bg:#f4f7f4;--card:#fff;--tx:#1d2b1f;--mut:#5f6f61;--ok:#1f8a4c;--bad:#c0392b;--bd:#dfe6df}
@media (prefers-color-scheme:dark){:root{--bg:#121714;--card:#1b221d;--tx:#e6efe7;--mut:#9aab9c;--bd:#2c372f}}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,sans-serif;background:var(--bg);color:var(--tx)}
main{max-width:900px;margin:0 auto;padding:16px}h1{font-size:1.4rem;margin:8px 0 4px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:12px 0}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:12px}
.v{font-size:1.6rem;font-weight:600}.l{color:var(--mut);font-size:.85rem}
.pill{display:inline-block;padding:3px 10px;border-radius:99px;color:#fff;font-size:.85rem}
table{width:100%;border-collapse:collapse;font-size:.9rem}td,th{padding:6px;border-bottom:1px solid var(--bd);text-align:left}
form.cfg{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}
label{font-size:.85rem;color:var(--mut)}input,select{width:100%;padding:8px;border:1px solid var(--bd);border-radius:6px;background:var(--bg);color:var(--tx)}
button{padding:10px 14px;border:0;border-radius:6px;background:var(--ok);color:#fff;font-weight:600;cursor:pointer}
button.red{background:var(--bad)}.msg{background:#fff3cd;color:#664d03;padding:8px 12px;border-radius:6px}
.wrap{overflow-x:auto}
</style></head><body><main>
<h1>Irrigacao Inteligente</h1>
<div>Dispositivo: <span id="st" class="pill" style="background:#888">carregando...</span> <span class="l" id="uc"></span></div>
{% if msg %}<p class="msg">{{msg}}</p>{% endif %}
<div class="grid">
 <div class="card"><div class="l">Temperatura</div><div class="v" id="temp">-</div></div>
 <div class="card"><div class="l">Umidade do solo</div><div class="v" id="solo">-</div></div>
 <div class="card"><div class="l">Luz (LDR)</div><div class="v" id="luz">-</div></div>
 <div class="card"><div class="l">Bomba</div><div class="v" id="bomba">-</div></div>
</div>
<div class="card" id="errs" style="display:none;border-color:var(--bad)"></div>

<h2>Comandos</h2>
<form method="post" action="/cmd" class="card" style="display:flex;gap:8px;flex-wrap:wrap;align-items:end">
 <div><label>Senha</label><input name="senha" type="password" required></div>
 <button name="acao" value="regar">Regar agora</button>
 <button name="acao" value="parar" class="red">Parar bomba</button>
</form>

<h2>Configuracao</h2>
<form method="post" action="/config" class="card cfg">
 <div><label>Intervalo de envio (s)</label><input name="intervalo" type="number" min="5" max="3600" value="{{c.intervalo}}"></div>
 <div><label>Tempo de rega (s)</label><input name="tempoRega" type="number" min="1" max="600" value="{{c.tempoRega}}"></div>
 <div><label>Limite seco (%)</label><input name="limSeco" type="number" min="0" max="100" value="{{c.limSeco}}"></div>
 <div><label>Limite molhado (%)</label><input name="limUmido" type="number" min="0" max="100" value="{{c.limUmido}}"></div>
 <div><label>Modo</label><select name="modo">
   {% for k,v in modos.items() %}<option value="{{k}}" {% if c.modo==k %}selected{% endif %}>{{v}}</option>{% endfor %}
 </select></div>
 <div><label>Velocidade bomba (-1 = potenciometro)</label><input name="vel" type="number" min="-1" max="255" value="{{c.vel}}"></div>
 <div><label>Senha</label><input name="senha" type="password" required></div>
 <div style="display:flex;align-items:end"><button>Salvar</button></div>
</form>

<h2>Eventos e erros</h2>
<div class="card wrap"><table id="ev"><tr><td>-</td></tr></table></div>
<h2>Ultimas leituras</h2>
<div class="card wrap"><table id="tb"></table></div>
</main>
<script>
const hora = s => new Date(s*1000).toLocaleTimeString('pt-BR');
async function atualizar(){
  try{
    const e = await (await fetch('/api/estado')).json();
    const st = document.getElementById('st');
    st.textContent = e.online ? 'ONLINE' : 'OFFLINE';
    st.style.background = e.online ? 'var(--ok)' : 'var(--bad)';
    document.getElementById('uc').textContent = e.ultimo_contato ? 'ultimo contato ' + hora(e.ultimo_contato) + ' | modo ' + e.modo_txt : 'nunca conectou';
    const u = e.ultima;
    if(u){
      temp.textContent = u.temp.toFixed(1) + ' °C'; solo.textContent = u.solo + ' %';
      luz.textContent = u.luz; bomba.textContent = u.bomba ? 'LIGADA' : 'desligada';
      const er = document.getElementById('errs');
      er.style.display = u.erros_txt.length ? 'block' : 'none';
      er.textContent = 'Atencao: ' + u.erros_txt.join(' | ');
    }
    ev.innerHTML = e.eventos.map(x => `<tr><td>${hora(x.em)}</td><td>${x.msg}</td></tr>`).join('') || '<tr><td>sem eventos</td></tr>';
    const ls = await (await fetch('/api/leituras?n=15')).json();
    tb.innerHTML = '<tr><th>Hora</th><th>Temp</th><th>Solo</th><th>Luz</th><th>Bomba</th><th>Erros</th></tr>' +
      ls.map(r => `<tr><td>${hora(r.em)}</td><td>${r.temp.toFixed(1)}</td><td>${r.solo}%</td><td>${r.luz}</td><td>${r.bomba?'on':'off'}</td><td>${r.erros||''}</td></tr>`).join('');
  }catch(err){ document.getElementById('st').textContent = 'sem conexao com o servidor'; }
}
atualizar(); setInterval(atualizar, 3000);
</script></body></html>"""


@app.route("/")
def painel():
    return render_template_string(PAINEL, c=get_cfg(), modos=MODOS, msg=request.args.get("msg"))


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
