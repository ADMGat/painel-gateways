#!/usr/bin/env python3
"""
Painel de Check-in de Gateways de Pagamento
Rode:  python3 painel_gateways.py     e abra  http://localhost:8000
Precisa apenas do Python 3.8+ (sem instalar nada).

O painel INICIA SOZINHO: visita varios sites AO MESMO TEMPO (20 por vez), le o
codigo publico da pagina inicial e procura marcas de Pagar.me, Cielo, Rede,
Getnet e maxiPago. A lista de sites fica no arquivo sites.txt (criado na
primeira vez). Use o botao para desativar/ativar quando quiser.
  SUCESSO    (verde):    o site respondeu e um gateway foi encontrado.
  FALHA      (vermelho): o site nao respondeu, deu erro ou nao mostra gateway.
  BLOQUEADOS (amarelo):  o site recusou o painel (403/429, anti-robo ou robots.txt).
"""
import base64, hmac, json, os, re, socket, ipaddress, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.parse import urlparse
from urllib.error import HTTPError
from urllib.robotparser import RobotFileParser

UA = "PainelCheckinGateways/1.0 (verificacao publica de gateways de pagamento)"
WORKERS = 20          # sites verificados ao mesmo tempo
INTERVALO = 300       # segundos entre uma rodada e outra enquanto ativo
MAX_SITES = 200

# Marcas procuradas no codigo da pagina. Ajuste/acrescente se precisar.
SIGS = {
    "Pagar.me": ["pagar.me", "pagarme"],
    "Cielo": ["cieloecommerce", "ecommerce.cielo", "api.cielo"],
    "Rede": ["userede.com.br", "e.rede.com.br", "api.userede"],
    "Getnet": ["getnet.com.br", "api.getnet", "checkout.getnet"],
    "maxiPago": ["maxipago"],
}
BLOCK_MARKS = ["just a moment...", "cf-browser-verification", "attention required! | cloudflare",
               "access denied", "px-captcha", "request unsuccessful. incapsula", "verifying you are human"]

SITES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sites.txt")
DEFAULT_SITES = ["magazineluiza.com.br", "americanas.com.br", "casasbahia.com.br", "kabum.com.br", "netshoes.com.br"]

def save_sites(sites):
    try:
        with open(SITES_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(sites) + "\n")
    except Exception:
        pass

def load_sites():
    try:
        with open(SITES_FILE, encoding="utf-8") as f:
            L = [l.strip() for l in f if l.strip()]
        if L:
            return L
    except Exception:
        pass
    save_sites(DEFAULT_SITES)
    return list(DEFAULT_SITES)

def host_ok(host):
    """Barra enderecos internos (protecao contra uso indevido do servidor)."""
    if os.environ.get("ALLOW_PRIVATE") == "1":
        return True
    try:
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except Exception:
        return False

class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not host_ok(urlparse(newurl).hostname or ""):
            raise Exception("redirecionamento barrado")
        return super().redirect_request(req, fp, code, msg, headers, newurl)

opener = build_opener(SafeRedirect)

def check(site):
    url = site if re.match(r"https?://", site, re.I) else "https://" + site
    p = urlparse(url)
    r = {"site": site, "status": "bad", "gateways": [], "reason": "", "ms": None}
    if not p.hostname or not host_ok(p.hostname):
        r["reason"] = "endereço inválido ou barrado pela proteção do painel"
        return r
    try:
        rp = RobotFileParser()  # respeita o robots.txt do site
        rq = Request("%s://%s/robots.txt" % (p.scheme, p.netloc), headers={"User-Agent": UA})
        try:
            with opener.open(rq, timeout=6) as f:
                rp.parse(f.read(200000).decode("utf-8", "ignore").splitlines())
            if not rp.can_fetch(UA, url):
                r["status"], r["reason"] = "blocked", "o robots.txt do site não permite a visita"
                return r
        except Exception:
            pass
        t0 = time.time()
        try:
            with opener.open(Request(url, headers={"User-Agent": UA, "Accept": "text/html"}), timeout=10) as f:
                body = f.read(1500000).decode("utf-8", "ignore").lower()
            r["ms"] = int((time.time() - t0) * 1000)
        except HTTPError as e:
            if e.code in (401, 403, 429):
                r["status"], r["reason"] = "blocked", "o site recusou a visita (HTTP %d)" % e.code
            else:
                r["reason"] = "o site retornou erro HTTP %d" % e.code
            return r
    except Exception as e:
        r["reason"] = "site não respondeu: " + str(e)[:80]
        return r
    found = [g for g, ks in SIGS.items() if any(k in body for k in ks)]
    r["gateways"] = found
    if found:
        r["status"], r["reason"] = "ok", "gateway encontrado"
    elif any(m in body for m in BLOCK_MARKS):
        r["status"], r["reason"] = "blocked", "proteção anti-robô (verificação do navegador)"
    else:
        r["reason"] = "respondeu, mas nenhum gateway aparece na página inicial"
    return r

S = {"active": False, "scanning": False, "results": {}, "total": 0, "done": 0, "next": None, "rounds": 0}
LOCK = threading.Lock()
STOP = threading.Event()

def loop(sites):
    while not STOP.is_set():
        with LOCK:
            S.update(scanning=True, done=0, total=len(sites), next=None)
        def work(s):
            return None if STOP.is_set() else check(s)
        with ThreadPoolExecutor(WORKERS) as ex:
            for f in as_completed([ex.submit(work, s) for s in sites]):
                r = f.result()
                if r:
                    with LOCK:
                        S["results"][r["site"]] = r
                        S["done"] += 1
        with LOCK:
            S["scanning"] = False
            S["rounds"] += 1
            S["next"] = time.time() + INTERVALO
        STOP.wait(INTERVALO)
    with LOCK:
        S.update(active=False, scanning=False, next=None)

def start(sites):
    with LOCK:
        if S["active"]:
            return
        S.update(active=True, results={}, rounds=0, done=0, total=len(sites))
    STOP.clear()
    threading.Thread(target=loop, args=(sites,), daemon=True).start()

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def send(self, code, body, ctype="application/json"):
        b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
    def auth_ok(self):
        """Pede usuario 'admin' e a senha de PAINEL_SENHA (se definida)."""
        senha = os.environ.get("PAINEL_SENHA")
        if not senha:
            return True
        try:
            tipo, cred = (self.headers.get("Authorization") or "").split(" ", 1)
            usr, _, pwd = base64.b64decode(cred).decode("utf-8").partition(":")
            if tipo.lower() == "basic" and hmac.compare_digest(usr, "admin") and hmac.compare_digest(pwd, senha):
                return True
        except Exception:
            pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Painel de Gateways"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False
    def do_GET(self):
        if self.path == "/health":
            return self.send(200, "ok", "text/plain")
        if not self.auth_ok():
            return
        if self.path == "/api/status":
            with LOCK:
                self.send(200, json.dumps(S))
        elif self.path == "/api/sites":
            self.send(200, json.dumps(load_sites()))
        elif self.path in ("/", "/index.html"):
            self.send(200, PAGE, "text/html")
        else:
            self.send(404, "{}")
    def do_POST(self):
        if not self.auth_ok():
            return
        n = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(n) or b"{}") if n else {}
        if self.path == "/api/start":
            sites = []
            for l in data.get("sites", []):
                l = str(l).strip()
                if l and l not in sites:
                    sites.append(l)
            sites = sites[:MAX_SITES]
            if not sites:
                return self.send(400, json.dumps({"erro": "lista vazia"}))
            save_sites(sites)
            start(sites)
            self.send(200, "{}")
        elif self.path == "/api/stop":
            STOP.set()
            self.send(200, "{}")
        else:
            self.send(404, "{}")

PAGE = r"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Painel de Check-in de Gateways</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--tx:#1b1f24;--mut:#667085;--bd:#e3e6ea;--ok:#12803c;--bad:#d92d20;--blk:#b54708;--acc:#2456d6}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#181b21;--tx:#eceff4;--mut:#98a2b3;--bd:#2a2f38;--ok:#4ade80;--bad:#f87171;--blk:#fbbf24;--acc:#7aa2ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font-family:system-ui,Segoe UI,Arial,sans-serif;line-height:1.45}
.w{max-width:960px;margin:0 auto;padding:20px 16px 40px}h1{font-size:22px;margin:0 0 4px}.sub{color:var(--mut);font-size:14px;margin:0 0 16px}
button{font:inherit;cursor:pointer;border-radius:10px;border:0;background:var(--acc);color:#fff;font-weight:600;padding:12px 22px}
button.on{background:var(--bad)}
textarea{width:100%;height:110px;margin:12px 0 4px;padding:10px;border-radius:10px;border:1px solid var(--bd);background:var(--card);color:var(--tx);font:13px ui-monospace,Consolas,monospace}
.row{display:flex;flex-wrap:wrap;gap:10px;align-items:center}.ind{color:var(--mut);font-size:13px}
.g{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin:14px 0}
.g div{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:10px;text-align:center}
.g b{display:block;font-size:24px}.g span{font-size:12px;color:var(--mut)}
.ok{color:var(--ok)}.bad{color:var(--bad)}.blk{color:var(--blk)}
h3{margin:22px 0 8px;font-size:16px}
.l{background:var(--card);border:1px solid var(--bd);border-radius:12px;max-height:300px;overflow:auto;font-size:14px}
.l div{padding:8px 12px;border-bottom:1px solid var(--bd)}.l div:last-child{border:0}
.m{color:var(--mut);font-size:12px}.e{color:var(--mut);padding:12px}
.tag{display:inline-block;font-size:12px;border:1px solid var(--ok);color:var(--ok);border-radius:99px;padding:0 8px;margin-left:6px}
</style></head><body><div class="w">
<h1>Painel de Check-in de Gateways</h1>
<p class="sub">Funciona sozinho: verifica vários sites ao mesmo tempo e mostra onde Pagar.me, Cielo, Rede, Getnet e maxiPago aparecem.</p>
<div class="row"><button id="t">Desativar</button><span class="ind" id="ind">Iniciando…</span></div>
<textarea id="sites" spellcheck="false"></textarea>
<div class="m">Um site por linha (até 200). A lista fica salva em sites.txt. Os que vêm de início são só exemplos: troque pelos seus.</div>
<div class="g">
<div><b class="ok" id="nOk">0</b><span>Sucesso</span></div>
<div><b class="bad" id="nBad">0</b><span>Falha</span></div>
<div><b class="blk" id="nBlk">0</b><span>Bloqueados</span></div>
<div><b id="c0">0</b><span>Pagar.me</span></div><div><b id="c1">0</b><span>Cielo</span></div>
<div><b id="c2">0</b><span>Rede</span></div><div><b id="c3">0</b><span>Getnet</span></div><div><b id="c4">0</b><span>maxiPago</span></div>
</div>
<h3 class="ok">Sucesso</h3><div class="l" id="lo"><div class="e">Nada ainda.</div></div>
<h3 class="bad">Falha</h3><div class="l" id="lb"><div class="e">Nada ainda.</div></div>
<h3 class="blk">Bloqueados</h3><div class="l" id="ll"><div class="e">Nada ainda.</div></div>
</div><script>
var GS=["Pagar.me","Cielo","Rede","Getnet","maxiPago"],act=false,NONE='<div class="e">Nada ainda.</div>';
function esc(s){return String(s).replace(/[&<>"']/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]})}
function post(u,d){return fetch(u,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(d||{})})}
function lista(a,cls){return a.length?a.map(function(r){return '<div><b class="'+cls+'">'+esc(r.site)+'</b> <span class="m">'+esc(r.reason)+'</span></div>'}).join(""):NONE}
document.getElementById("t").onclick=function(){
  if(act){post("/api/stop")}
  else{post("/api/start",{sites:document.getElementById("sites").value.split("\n")}).then(function(r){if(!r.ok)alert("Coloque ao menos um site na lista.")})}
  setTimeout(poll,300)};
function poll(){fetch("/api/status").then(function(r){return r.json()}).then(function(s){
  act=s.active;var b=document.getElementById("t");b.textContent=act?"Desativar":"Ativar";b.className=act?"on":"";
  var i=document.getElementById("ind");
  i.textContent=!act?"Desativado":s.scanning?("Verificando… "+s.done+" de "+s.total):"Ativo · nova rodada em "+Math.max(0,Math.round(s.next-Date.now()/1000))+" s";
  var ok=[],bad=[],blk=[],c=[0,0,0,0,0];
  Object.keys(s.results).forEach(function(k){var r=s.results[k];
    if(r.status==="ok"){ok.push(r);r.gateways.forEach(function(g){c[GS.indexOf(g)]++})}else if(r.status==="blocked")blk.push(r);else bad.push(r)});
  document.getElementById("nOk").textContent=ok.length;document.getElementById("nBad").textContent=bad.length;document.getElementById("nBlk").textContent=blk.length;
  c.forEach(function(n,j){document.getElementById("c"+j).textContent=n});
  document.getElementById("lo").innerHTML=ok.length?ok.map(function(r){return '<div><b class="ok">'+esc(r.site)+'</b>'+r.gateways.map(function(g){return '<span class="tag">'+g+'</span>'}).join("")+' <span class="m">'+r.ms+' ms</span></div>'}).join(""):NONE;
  document.getElementById("lb").innerHTML=lista(bad,"bad");
  document.getElementById("ll").innerHTML=lista(blk,"blk");
}).catch(function(){document.getElementById("ind").textContent="Servidor desligado"})}
fetch("/api/sites").then(function(r){return r.json()}).then(function(a){document.getElementById("sites").value=a.join("\n")});
setInterval(poll,1500);poll();
</script></body></html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    # Em hospedagem (PORT definida pelo servico) aceita acesso da internet, mas so com senha.
    em_nuvem = bool(os.environ.get("PORT") or os.environ.get("RENDER"))
    host = (os.environ.get("HOST") or "").strip() or ("0.0.0.0" if em_nuvem else "127.0.0.1")
    if host != "127.0.0.1" and not os.environ.get("PAINEL_SENHA"):
        raise SystemExit("Defina a variavel PAINEL_SENHA antes de colocar o painel na internet.")
    print("Painel v3 iniciando em %s:%d | senha %s" % (host, port, "definida" if os.environ.get("PAINEL_SENHA") else "NAO definida"), flush=True)
    if os.environ.get("AUTO", "1") == "1":
        start(load_sites()[:MAX_SITES])
        print("Check-in automático iniciado com os sites de sites.txt", flush=True)
    ThreadingHTTPServer((host, port), H).serve_forever()
