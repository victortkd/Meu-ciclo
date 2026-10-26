import os, sqlite3, math
from datetime import date, timedelta
from functools import wraps
from flask import (Flask, g, request, redirect, url_for, session,
                   flash, jsonify, render_template, abort)
from jinja2 import DictLoader
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
IS_PROD = os.environ.get("FLASK_ENV") == "production"
app.secret_key = os.environ.get("SECRET_KEY") or ("" if IS_PROD else "dev-key-local")
if not app.secret_key:
    raise RuntimeError("Defina a variável SECRET_KEY em produção.")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=IS_PROD, PREFERRED_URL_SCHEME="https" if IS_PROD else "http")
# atrás do proxy do provedor (HTTPS) para gerar URLs corretas no login Google
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
# Em hospedagem, aponte DATABASE_PATH para um disco/volume persistente (ex.: /data/ciclo.db)
DB = os.environ.get("DATABASE_PATH") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "ciclo.db")
PMS_DAYS = 7  # a TPM costuma comecar ~7 dias antes da menstruacao

# ---------- Google OAuth (opcional) ----------
GOOGLE_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
oauth = None
if GOOGLE_ID and GOOGLE_SECRET:
    from authlib.integrations.flask_client import OAuth
    oauth = OAuth(app)
    oauth.register(
        "google", client_id=GOOGLE_ID, client_secret=GOOGLE_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"})

# ---------- Banco SQLite ----------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT,
  pw_hash TEXT, google_sub TEXT);
CREATE TABLE IF NOT EXISTS women(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL,
  cycle_len INTEGER DEFAULT 28, period_len INTEGER DEFAULT 5);
CREATE TABLE IF NOT EXISTS cycles(
  id INTEGER PRIMARY KEY, woman_id INTEGER NOT NULL, start_date TEXT NOT NULL,
  kind TEXT DEFAULT 'menstruacao', UNIQUE(woman_id, start_date));
"""

def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d:
        d.close()

os.makedirs(os.path.dirname(os.path.abspath(DB)), exist_ok=True)
with sqlite3.connect(DB) as _c:
    _c.execute("PRAGMA journal_mode=WAL")
    _c.executescript(SCHEMA)

@app.route("/health")
def health():
    return "ok"

def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if "uid" not in session:
            return redirect(url_for("login"))
        return f(*a, **k)
    return w

def get_woman(wid):
    w = db().execute("SELECT * FROM women WHERE id=? AND user_id=?",
                     (wid, session["uid"])).fetchone()
    if not w:
        abort(404)
    return w

# ---------- Calculo do ciclo ----------
FERT = {-5: 10, -4: 16, -3: 14, -2: 27, -1: 31, 0: 33, 1: 8}

def get_starts(wid):
    rows = db().execute("SELECT start_date FROM cycles WHERE woman_id=? ORDER BY start_date",
                        (wid,)).fetchall()
    return [date.fromisoformat(r["start_date"]) for r in rows]

def avg_cycle(w, starts):
    diffs = [(b - a).days for a, b in zip(starts, starts[1:])]
    diffs = [d for d in diffs if 18 <= d <= 45][-6:]
    return round(sum(diffs) / len(diffs)) if diffs else w["cycle_len"]

def build(w, starts, horizon=150):
    today = date.today()
    C, P = avg_cycle(w, starts), w["period_len"]
    cycles = []
    for i, s in enumerate(starts):
        L = (starts[i + 1] - s).days if i + 1 < len(starts) else C
        cycles.append((s, L if 18 <= L <= 45 else C))
    nxt = cycles[-1][0] + timedelta(days=cycles[-1][1])
    while nxt < today + timedelta(days=horizon):
        cycles.append((nxt, C))
        nxt += timedelta(days=C)

    days, score, events = {}, {}, []
    for s, L in cycles:
        ov = s + timedelta(days=L - 14)
        for k in range(L):
            d = s + timedelta(days=k)
            days[d] = ["lutea" if d > ov else "folic", False]
        for k in range(P):
            days[s + timedelta(days=k)][0] = "mens"
        for k in range(PMS_DAYS):
            d = s + timedelta(days=L - PMS_DAYS + k)
            days[d][0] = "tpm"
        for k in range(-5, 2):
            days[ov + timedelta(days=k)][0] = "fertil"
        days[ov][0] = "ovul"
        for k in range(-2, 2):
            days[ov + timedelta(days=k)][1] = True
        # indices 0-100 para o grafico
        for k in range(L):
            d = s + timedelta(days=k)
            dd = (d - ov).days
            fert = round(FERT.get(dd, 0) / 33 * 100)
            libido = round(100 * math.exp(-((dd + 0.5) / 2.4) ** 2))
            tpm = 0
            if d >= s + timedelta(days=L - PMS_DAYS):
                tpm = round(40 + 60 * (d - (s + timedelta(days=L - PMS_DAYS))).days / (PMS_DAYS - 1))
            score[d] = (fert, libido, tpm)
        events.append({
            "mens": (s, s + timedelta(days=P - 1)),
            "tpm": (s + timedelta(days=L - PMS_DAYS), s + timedelta(days=L - 1)),
            "fertil": (ov - timedelta(days=5), ov + timedelta(days=1)),
            "hot": (ov - timedelta(days=2), ov + timedelta(days=1)),
        })
    nxt_ev = {}
    for kind in ("mens", "tpm", "fertil", "hot"):
        for e in events:
            if e[kind][1] >= today:
                nxt_ev[kind] = [e[kind][0].isoformat(), e[kind][1].isoformat()]
                break
    return C, days, score, nxt_ev

# ---------- Autenticacao ----------
def do_login(u):
    session.clear()
    session["uid"], session["uname"] = u["id"], u["name"] or u["email"]

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        pw, name = request.form["password"], request.form.get("name", "").strip()
        if "@" not in email or len(pw) < 6:
            flash("Informe um email válido e senha com 6+ caracteres.")
        elif db().execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            flash("Este email já está cadastrado.")
        else:
            cur = db().execute("INSERT INTO users(email,name,pw_hash) VALUES(?,?,?)",
                               (email, name, generate_password_hash(pw)))
            db().commit()
            do_login({"id": cur.lastrowid, "name": name, "email": email})
            return redirect(url_for("home"))
    return render_template("auth.html", mode="register", google=bool(oauth))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        u = db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if u and u["pw_hash"] and check_password_hash(u["pw_hash"], request.form["password"]):
            do_login(u)
            return redirect(url_for("home"))
        flash("Email ou senha incorretos.")
    return render_template("auth.html", mode="login", google=bool(oauth))

@app.route("/auth/google")
def google_login():
    if not oauth:
        abort(404)
    return oauth.google.authorize_redirect(url_for("google_cb", _external=True))

@app.route("/auth/google/callback")
def google_cb():
    if not oauth:
        abort(404)
    info = oauth.google.authorize_access_token()["userinfo"]
    email = info["email"].lower()
    u = db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not u:
        cur = db().execute("INSERT INTO users(email,name,google_sub) VALUES(?,?,?)",
                           (email, info.get("name"), info["sub"]))
        db().commit()
        u = {"id": cur.lastrowid, "name": info.get("name"), "email": email}
    elif not u["google_sub"]:
        db().execute("UPDATE users SET google_sub=? WHERE id=?", (info["sub"], u["id"]))
        db().commit()
    do_login(u)
    return redirect(url_for("home"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ---------- Telas ----------
@app.route("/")
@login_required
def home():
    women = db().execute("SELECT * FROM women WHERE user_id=? ORDER BY name",
                         (session["uid"],)).fetchall()
    sel, hist = None, []
    if women:
        wid = request.args.get("w", type=int) or women[0]["id"]
        sel = get_woman(wid)
        hist = db().execute("SELECT * FROM cycles WHERE woman_id=? ORDER BY start_date DESC",
                            (wid,)).fetchall()
    return render_template("dash.html", women=women, w=sel, hist=hist, today=date.today().isoformat())

@app.route("/women", methods=["POST"])
@login_required
def add_woman():
    name = request.form["name"].strip()
    if name:
        cl = min(max(request.form.get("cycle_len", 28, type=int), 21), 40)
        pl = min(max(request.form.get("period_len", 5, type=int), 2), 10)
        cur = db().execute("INSERT INTO women(user_id,name,cycle_len,period_len) VALUES(?,?,?,?)",
                           (session["uid"], name, cl, pl))
        db().commit()
        return redirect(url_for("home", w=cur.lastrowid))
    return redirect(url_for("home"))

@app.route("/women/<int:wid>/delete", methods=["POST"])
@login_required
def del_woman(wid):
    get_woman(wid)
    db().execute("DELETE FROM cycles WHERE woman_id=?", (wid,))
    db().execute("DELETE FROM women WHERE id=?", (wid,))
    db().commit()
    return redirect(url_for("home"))

@app.route("/women/<int:wid>/mark", methods=["POST"])
@login_required
def mark(wid):
    get_woman(wid)
    try:
        d = date.fromisoformat(request.form["date"])
    except ValueError:
        flash("Data inválida.")
        return redirect(url_for("home", w=wid))
    kind = request.form["kind"]
    if kind == "tpm":  # estima o inicio da menstruacao a partir do dia da TPM
        d += timedelta(days=PMS_DAYS)
    db().execute("INSERT OR IGNORE INTO cycles(woman_id,start_date,kind) VALUES(?,?,?)",
                 (wid, d.isoformat(), kind))
    db().commit()
    return redirect(url_for("home", w=wid))

@app.route("/cycles/<int:cid>/delete", methods=["POST"])
@login_required
def del_cycle(cid):
    c = db().execute("SELECT c.* FROM cycles c JOIN women w ON w.id=c.woman_id "
                     "WHERE c.id=? AND w.user_id=?", (cid, session["uid"])).fetchone()
    if not c:
        abort(404)
    db().execute("DELETE FROM cycles WHERE id=?", (cid,))
    db().commit()
    return redirect(url_for("home", w=c["woman_id"]))

@app.route("/api/women/<int:wid>")
@login_required
def api(wid):
    w = get_woman(wid)
    starts = get_starts(wid)
    if not starts:
        return jsonify(empty=True)
    C, days, score, nxt = build(w, starts)
    today = date.today()
    rng = [today + timedelta(days=k) for k in range(-14, 46)]
    return jsonify(
        empty=False, today=today.isoformat(), cycle=C, next=nxt,
        days={d.isoformat(): v for d, v in days.items()},
        chart={"labels": [d.strftime("%d/%m") for d in rng],
               "fert": [score.get(d, (0, 0, 0))[0] for d in rng],
               "libido": [score.get(d, (0, 0, 0))[1] for d in rng],
               "tpm": [score.get(d, (0, 0, 0))[2] for d in rng]})

# ---------- Templates ----------
BASE = r"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Meu Ciclo</title>
<style>
*{box-sizing:border-box}body{margin:0;font-family:system-ui,sans-serif;background:#fdf2f8;color:#3b0a2a}
header{background:#be185d;color:#fff;padding:12px 20px;display:flex;justify-content:space-between;align-items:center}
header a{color:#fff}main{max-width:1100px;margin:auto;padding:16px}
.card{background:#fff;border-radius:14px;padding:16px;box-shadow:0 2px 8px #0001;margin-bottom:14px}
input,select,button{font:inherit;padding:8px 10px;border-radius:8px;border:1px solid #d9b3c8}
button,.btn{background:#be185d;color:#fff;border:0;cursor:pointer;text-decoration:none;display:inline-block;padding:9px 14px;border-radius:8px}
button.sec{background:#fff;color:#be185d;border:1px solid #be185d}button.x{background:none;color:#999;padding:2px 8px}
.flash{background:#fee2e2;padding:10px;border-radius:8px;margin-bottom:12px}
.grid{display:grid;grid-template-columns:230px 1fr;gap:16px}@media(max-width:800px){.grid{grid-template-columns:1fr}}
.wl a{display:block;padding:8px 10px;border-radius:8px;color:inherit;text-decoration:none}.wl a.on{background:#fce7f3;font-weight:700}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}
.stat{background:#fdf2f8;border-radius:10px;padding:10px}.stat small{color:#7a4b65}.stat b{display:block;font-size:1.05rem}
.stat.hot{background:linear-gradient(135deg,#ec4899,#f97316);color:#fff}.stat.hot small{color:#ffe4ef}
.cal{display:grid;grid-template-columns:repeat(7,1fr);gap:4px;text-align:center}
.cal .h{font-size:.75rem;color:#7a4b65}.cal .d{padding:8px 0;border-radius:8px;background:#f5f5f5;font-size:.9rem;min-height:40px}
.mens{background:#fecdd3!important}.tpm{background:#fde68a!important}.fertil{background:#bbf7d0!important}
.ovul{background:#5eead4!important;font-weight:700}.today{outline:2px solid #3b0a2a}
.hot{box-shadow:0 0 0 2px #ec4899,0 0 10px #ec4899}
.legend span{display:inline-block;margin:4px 10px 0 0;font-size:.85rem}.legend i{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:4px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
</style></head><body>
<header><b>🌸 Meu Ciclo</b><span>{% if session.uid %}{{ session.uname }} · <a href="/logout">Sair</a>{% endif %}</span></header>
<main>{% for m in get_flashed_messages() %}<div class="flash">{{ m }}</div>{% endfor %}{% block body %}{% endblock %}</main>
</body></html>"""

AUTH = r"""{% extends "base.html" %}{% block body %}
<div class="card" style="max-width:380px;margin:30px auto">
<h2>{{ 'Criar conta' if mode=='register' else 'Entrar' }}</h2>
<form method="post" class="row" style="flex-direction:column;align-items:stretch">
{% if mode=='register' %}<input name="name" placeholder="Seu nome">{% endif %}
<input name="email" type="email" placeholder="Email" required>
<input name="password" type="password" placeholder="Senha (mín. 6)" required>
<button>{{ 'Cadastrar' if mode=='register' else 'Entrar' }}</button></form>
<p style="text-align:center">ou</p>
{% if google %}<a class="btn sec" style="width:100%;text-align:center" href="/auth/google">Continuar com Google</a>
{% else %}<button class="sec" style="width:100%" disabled title="Defina GOOGLE_CLIENT_ID e GOOGLE_CLIENT_SECRET">Continuar com Google (não configurado)</button>{% endif %}
<p>{% if mode=='register' %}Já tem conta? <a href="/login">Entrar</a>{% else %}Novo aqui? <a href="/register">Criar conta</a>{% endif %}</p>
</div>{% endblock %}"""

DASH = r"""{% extends "base.html" %}{% block body %}
<div class="grid"><aside>
<div class="card wl"><b>Mulheres</b>
{% for x in women %}<a href="/?w={{ x.id }}" class="{{ 'on' if w and w.id==x.id }}">{{ x.name }}</a>{% endfor %}
{% if not women %}<p>Nenhuma cadastrada.</p>{% endif %}</div>
<form class="card row" method="post" action="/women" style="flex-direction:column;align-items:stretch">
<b>Adicionar mulher</b><input name="name" placeholder="Nome" required>
<label>Ciclo (dias)<input name="cycle_len" type="number" value="28" min="21" max="40" style="width:100%"></label>
<label>Menstruação (dias)<input name="period_len" type="number" value="5" min="2" max="10" style="width:100%"></label>
<button>Adicionar</button></form></aside>
<section>
{% if w %}
<div class="card"><div class="row" style="justify-content:space-between"><h2 style="margin:0">{{ w.name }}</h2>
<form method="post" action="/women/{{ w.id }}/delete" onsubmit="return confirm('Excluir {{ w.name }} e todo o histórico?')"><button class="sec">Excluir</button></form></div>
<form method="post" action="/women/{{ w.id }}/mark" class="row" style="margin-top:10px">
<select name="kind"><option value="menstruacao">1º dia da menstruação</option><option value="tpm">Dia em que a TPM começou</option></select>
<input type="date" name="date" value="{{ today }}" required><button>Marcar</button></form>
<small>Se marcar a TPM, a menstruação é estimada para {{ 7 }} dias depois. Quanto mais ciclos marcados, mais precisa a previsão.</small></div>
<div id="empty" class="card" style="display:none">Marque a primeira data para ver as previsões.</div>
<div id="out" style="display:none">
<div class="card"><div class="stats" id="stats"></div></div>
<div class="card"><div class="row" style="justify-content:space-between"><button class="sec" onclick="mv(-1)">◀</button><b id="mt"></b><button class="sec" onclick="mv(1)">▶</button></div>
<div class="cal" id="cal" style="margin-top:10px"></div>
<div class="legend"><span><i style="background:#fecdd3"></i>Menstruação</span><span><i style="background:#fde68a"></i>TPM</span><span><i style="background:#bbf7d0"></i>Fértil</span><span><i style="background:#5eead4"></i>Ovulação</span><span><i style="background:#fff;box-shadow:0 0 0 2px #ec4899"></i>🔥 Mais fogosa</span></div></div>
<div class="card"><canvas id="ch" height="110"></canvas></div></div>
<div class="card"><b>Histórico</b>{% for c in hist %}<div class="row">{{ c.start_date[8:10] }}/{{ c.start_date[5:7] }}/{{ c.start_date[:4] }} · {{ 'estimado pela TPM' if c.kind=='tpm' else 'menstruação' }}
<form method="post" action="/cycles/{{ c.id }}/delete"><button class="x">✕</button></form></div>{% else %}<p>Sem registros.</p>{% endfor %}</div>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script>
const WID={{ w.id }},NAMES={mens:'Menstruação',fertil:'Período fértil',ovul:'Ovulação',tpm:'TPM',folic:'Fase folicular',lutea:'Fase lútea'};
let data,view=new Date(),chart;view.setDate(1);
const iso=d=>d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');
const br=s=>s.slice(8,10)+'/'+s.slice(5,7);
const rg=a=>a?br(a[0])+' a '+br(a[1]):'—';
function stat(t,v,c){return '<div class="stat '+(c||'')+'"><small>'+t+'</small><b>'+v+'</b></div>'}
async function load(){
 data=await (await fetch('/api/women/'+WID)).json();
 if(data.empty){document.getElementById('empty').style.display='block';return}
 document.getElementById('out').style.display='block';
 const td=data.days[data.today],n=data.next;
 document.getElementById('stats').innerHTML=
  stat('Hoje',(td?NAMES[td[0]]:'—')+(td&&td[1]?' 🔥':''),td&&td[1]?'hot':'')+
  stat('Próx. menstruação',rg(n.mens))+stat('Próxima TPM',rg(n.tpm))+
  stat('Próximo período fértil',rg(n.fertil))+stat('🔥 Dias mais fogosa',rg(n.hot),'hot')+
  stat('Ciclo médio',data.cycle+' dias');
 cal();draw();
}
function mv(k){view.setMonth(view.getMonth()+k);cal()}
function cal(){
 const y=view.getFullYear(),m=view.getMonth();
 document.getElementById('mt').textContent=view.toLocaleDateString('pt-BR',{month:'long',year:'numeric'});
 let h=['D','S','T','Q','Q','S','S'].map(x=>'<div class="h">'+x+'</div>').join('');
 for(let i=0;i<new Date(y,m,1).getDay();i++)h+='<div></div>';
 for(let d=1;d<=new Date(y,m+1,0).getDate();d++){
  const k=iso(new Date(y,m,d)),v=data.days[k];
  h+='<div class="d '+(v?v[0]+(v[1]?' hot':''):'')+(k==data.today?' today':'')+'" title="'+(v?NAMES[v[0]]:'')+'">'+d+(v&&v[1]?'🔥':'')+'</div>';
 }
 document.getElementById('cal').innerHTML=h;
}
function draw(){
 const c=data.chart;if(chart)chart.destroy();
 const ds=(l,d,col,x)=>Object.assign({label:l,data:d,borderColor:col,tension:.35,pointRadius:0},x||{});
 chart=new Chart(document.getElementById('ch'),{type:'line',data:{labels:c.labels,datasets:[
  ds('Fertilidade',c.fert,'#16a34a'),
  ds('Libido 🔥',c.libido,'#ec4899',{borderWidth:3,fill:true,backgroundColor:'rgba(236,72,153,.15)'}),
  ds('TPM',c.tpm,'#f59e0b')]},
  options:{scales:{y:{min:0,max:100,title:{display:true,text:'Índice (0-100)'}}},plugins:{title:{display:true,text:'Próximas semanas'}}}});
}
load();
</script>
{% else %}<div class="card">Adicione uma mulher ao lado para começar.</div>{% endif %}
</section></div>{% endblock %}"""

app.jinja_loader = DictLoader({"base.html": BASE, "auth.html": AUTH, "dash.html": DASH})

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
