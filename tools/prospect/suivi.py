"""Tableau de bord de suivi : voir où en est chaque prospect, et le marquer.

`marquer envoye --tous` suffit le premier jour. Dès la deuxième semaine, il faut
savoir qui a répondu, qui attend une relance et qui n'a jamais eu d'adresse — et
ça, une commande ne le montre pas.

Un serveur local, servi sur 127.0.0.1 uniquement : les coordonnées des prospects
ne sortent pas de la machine. Bibliothèque standard seule, comme le reste.
"""

from __future__ import annotations

import http.server
import json
import socketserver
import sqlite3
import webbrowser
from pathlib import Path

from . import config as config_mod
from . import store

# Les verdicts qu'on démarche. `correct` et `non_auditable` n'ont rien à faire
# ici : le premier n'a pas de problème, du second on ne sait rien.
VERDICTS_SUIVIS = ("absent", "obsolete", "injoignable")

STATUTS = {"brouillon": "Brouillon", "envoye": "Envoyé", "repondu": "A répondu"}


def _lignes(con: sqlite3.Connection, cfg: dict) -> list[dict]:
    """Un prospect par ligne, avec son dernier mail — pas un par mail.

    Un prospect relancé deux fois a trois lignes dans `emails` ; ce qui intéresse
    l'œil, c'est son état courant.
    """
    d1 = int(cfg["quotas"].get("delai_relance_1_jours") or 4)
    d2 = int(cfg["quotas"].get("delai_relance_2_jours") or 9)
    rows = con.execute(
        """
        SELECT e.place_id, e.nom, e.categorie, e.ville, e.telephone, e.note, e.nb_avis,
               e.maps_url, a.verdict,
               COALESCE(m.destinataire, (
                   SELECT email FROM contacts
                   WHERE place_id = e.place_id AND email IS NOT NULL AND email <> ''
                   ORDER BY generique DESC, meme_domaine DESC, rowid LIMIT 1
               )) AS destinataire,
               m.type AS mail_type, m.statut AS mail_statut, m.objet,
               m.envoye_le, m.cree_le,
               CAST(julianday('now') - julianday(m.envoye_le) AS INTEGER) AS jours_depuis
        FROM etablissements e
        JOIN audits a USING(place_id)
        LEFT JOIN emails m ON m.id = (
            SELECT id FROM emails WHERE place_id = e.place_id
            ORDER BY COALESCE(envoye_le, cree_le) DESC, id DESC LIMIT 1)
        WHERE a.verdict IN (?, ?, ?)
        """,
        VERDICTS_SUIVIS,
    ).fetchall()

    exclus = {r[0] for r in con.execute("SELECT valeur FROM exclusions")}
    out = []
    for r in rows:
        d = dict(r)
        email = (d.get("destinataire") or "").strip()
        d["exclu"] = bool(email and email in exclus)
        d["canal"] = "mail" if email else "telephone"
        d["mail_statut"] = d.get("mail_statut") or "brouillon"

        # Relance due : le dernier message est parti, personne n'a répondu, et le
        # délai est passé. Le rang se lit sur le type du dernier mail envoyé.
        d["relance_due"] = ""
        j = d.get("jours_depuis")
        if d["mail_statut"] == "envoye" and isinstance(j, int) and not d["exclu"]:
            t = d.get("mail_type") or ""
            if t == "relance_1" and j >= (d2 - d1):
                d["relance_due"] = "R2"
            elif t == "relance_2":
                d["relance_due"] = ""
            elif not t.startswith("relance") and j >= d1:
                d["relance_due"] = "R1"
        out.append(d)

    # Les plus notés d'abord : à effort égal, on appelle celui qui a le plus à perdre.
    out.sort(key=lambda x: (-(x.get("nb_avis") or 0), x.get("nom") or ""))
    return out


def _compteurs(lignes: list[dict], cfg: dict) -> dict:
    quota = int(cfg["quotas"].get("max_emails_par_jour") or 0)
    return {
        "total": len(lignes),
        "brouillon": sum(1 for l in lignes if l["mail_statut"] == "brouillon"),
        "envoye": sum(1 for l in lignes if l["mail_statut"] == "envoye"),
        "repondu": sum(1 for l in lignes if l["mail_statut"] == "repondu"),
        "a_appeler": sum(1 for l in lignes if l["canal"] == "telephone" and not l["exclu"]),
        "relances": sum(1 for l in lignes if l["relance_due"]),
        "quota": quota,
    }


def _marquer(con: sqlite3.Connection, place_id: str, statut: str) -> int:
    """Mêmes règles que la commande `marquer` : un seul chemin d'écriture possible."""
    if statut == "envoye":
        n = con.execute(
            """UPDATE emails SET statut='envoye', envoye_le=datetime('now')
               WHERE place_id=? AND statut='brouillon'""", (place_id,)).rowcount
    elif statut in ("repondu", "brouillon"):
        n = con.execute(
            """UPDATE emails SET statut=? WHERE place_id=?
               AND statut IN ('brouillon','envoye')""", (statut, place_id)).rowcount
    else:
        raise ValueError(f"statut inconnu : {statut}")
    con.commit()
    return n


def _stopper(con: sqlite3.Connection, place_id: str) -> int:
    """« STOP » demandé : l'adresse sort des fichiers, définitivement."""
    adresses = {r[0].strip() for r in con.execute(
        "SELECT email FROM contacts WHERE place_id=? AND email IS NOT NULL AND email <> ''",
        (place_id,)) if (r[0] or "").strip()}
    adresses |= {r[0].strip() for r in con.execute(
        "SELECT destinataire FROM emails WHERE place_id=? AND destinataire IS NOT NULL",
        (place_id,)) if (r[0] or "").strip()}
    if not adresses:
        return 0
    # Un prospect peut avoir plusieurs adresses ; n'en exclure qu'une le laisserait
    # joignable par les autres, et « STOP » ne veut pas dire « moins souvent ».
    for a in adresses:
        con.execute("INSERT OR REPLACE INTO exclusions (valeur, motif, ajoute_le) "
                    "VALUES (?, ?, datetime('now'))", (a, "demande de désinscription"))
    con.commit()
    return len(adresses)


PAGE = """<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Suivi de prospection — 770LAB</title>
<style>
 :root{--bg:#fbfaf8;--fg:#1a1a1a;--mut:#6b6b6b;--line:#e6e3de;--card:#fff;
       --accent:#b4472f;--ok:#2f7d4f;--warn:#a9762d}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--fg);
      font:15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
 header{padding:28px 32px 18px;border-bottom:1px solid var(--line);background:var(--card)}
 h1{margin:0 0 4px;font-size:22px;letter-spacing:-.02em}
 .sub{color:var(--mut);font-size:14px}
 .kpis{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}
 .kpi{background:var(--bg);border:1px solid var(--line);border-radius:10px;
      padding:10px 14px;min-width:104px}
 .kpi b{display:block;font-size:21px;line-height:1.2}
 .kpi span{color:var(--mut);font-size:12px}
 .kpi.on{border-color:var(--accent)}
 main{padding:22px 32px 60px}
 .filtres{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}
 .filtres button{background:var(--card);border:1px solid var(--line);border-radius:999px;
   padding:6px 14px;cursor:pointer;font-size:13px;color:var(--fg)}
 .filtres button.on{background:var(--fg);color:var(--card);border-color:var(--fg)}
 .wrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:12px}
 table{border-collapse:collapse;width:100%;min-width:940px}
 th,td{text-align:left;padding:11px 14px;border-bottom:1px solid var(--line);
       font-size:14px;vertical-align:middle}
 th{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);
    font-weight:600;background:var(--bg);position:sticky;top:0}
 tr:last-child td{border-bottom:0}
 .nom{font-weight:600}
 .meta{color:var(--mut);font-size:12.5px}
 .tag{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;
      border:1px solid var(--line)}
 .v-absent{color:#8f3520;background:#fdeeea;border-color:#f2c9bf}
 .v-obsolete{color:#7a5410;background:#fdf4e3;border-color:#efdcb4}
 .v-injoignable{color:#4a4a4a;background:#f1f0ee;border-color:#ddd}
 .s-envoye{color:var(--ok)}.s-repondu{color:var(--ok);font-weight:600}
 .s-brouillon{color:var(--mut)}
 .due{color:var(--accent);font-weight:600}
 .exclu{opacity:.45}
 .act{display:flex;gap:6px}
 .act button{border:1px solid var(--line);background:var(--card);border-radius:7px;
   padding:5px 10px;cursor:pointer;font-size:12.5px;white-space:nowrap}
 .act button:hover{border-color:var(--fg)}
 .act button.p{background:var(--fg);color:var(--card);border-color:var(--fg)}
 a{color:var(--accent)}
 .vide{padding:40px;text-align:center;color:var(--mut)}
 #flash{position:fixed;left:50%;transform:translateX(-50%);bottom:26px;background:var(--fg);
   color:#fff;padding:10px 18px;border-radius:8px;font-size:14px;opacity:0;
   transition:opacity .2s;pointer-events:none}
 #flash.on{opacity:1}
</style>
<header>
  <h1>Suivi de prospection</h1>
  <div class="sub">770LAB · les données restent sur cette machine</div>
  <div class="kpis" id="kpis"></div>
</header>
<main>
  <div class="filtres" id="filtres"></div>
  <div class="wrap"><table>
    <thead><tr>
      <th>Établissement</th><th>Verdict</th><th>Canal</th><th>Statut</th>
      <th>Relance</th><th>Actions</th>
    </tr></thead>
    <tbody id="corps"></tbody>
  </table></div>
  <div class="vide" id="vide" hidden>Rien dans ce filtre.</div>
</main>
<div id="flash"></div>
<script>
let DATA=[], FILTRE='tous';
const FILTRES=[['tous','Tous'],['brouillon','À envoyer'],['envoye','Envoyés'],
               ['relance','Relance due'],['telephone','À appeler'],['repondu','Ont répondu']];

function flash(t){const f=document.getElementById('flash');f.textContent=t;
  f.classList.add('on');setTimeout(()=>f.classList.remove('on'),1800)}

function garde(l){
  if(FILTRE==='tous')return true;
  if(FILTRE==='relance')return !!l.relance_due;
  if(FILTRE==='telephone')return l.canal==='telephone'&&!l.exclu;
  return l.mail_statut===FILTRE;
}

function rendre(){
  const c=document.getElementById('corps'); c.innerHTML='';
  const vus=DATA.filter(garde);
  document.getElementById('vide').hidden=vus.length>0;
  for(const l of vus){
    const tr=document.createElement('tr');
    if(l.exclu)tr.className='exclu';
    const note=l.note?`${String(l.note).replace('.',',')}/5 · ${l.nb_avis} avis`:'—';
    const canal=l.canal==='mail'
      ? `<span class="meta">${l.destinataire}</span>`
      : `<span class="meta">☎ ${l.telephone||'pas de numéro'}</span>`;
    const st=l.mail_statut;
    const stTxt={brouillon:'Brouillon',envoye:'Envoyé',repondu:'A répondu'}[st]||st;
    const depuis=(st==='envoye'&&l.jours_depuis!=null)?` <span class="meta">· J+${l.jours_depuis}</span>`:'';
    tr.innerHTML=`
      <td><div class="nom">${l.nom||''}</div>
          <div class="meta">${l.categorie||''} · ${l.ville||''} · ${note}</div></td>
      <td><span class="tag v-${l.verdict}">${l.verdict}</span></td>
      <td>${canal}</td>
      <td><span class="s-${st}">${stTxt}</span>${depuis}</td>
      <td>${l.relance_due?`<span class="due">${l.relance_due} due</span>`:'<span class="meta">—</span>'}</td>
      <td><div class="act">
        ${st==='brouillon'&&l.canal==='mail'?`<button class="p" data-a="envoye" data-p="${l.place_id}">Envoyé</button>`:''}
        ${st!=='repondu'?`<button data-a="repondu" data-p="${l.place_id}">A répondu</button>`:''}
        ${l.canal==='mail'&&!l.exclu?`<button data-a="stop" data-p="${l.place_id}">STOP</button>`:''}
        ${l.maps_url?`<a class="meta" href="${l.maps_url}" target="_blank" rel="noopener">fiche</a>`:''}
      </div></td>`;
    c.appendChild(tr);
  }
}

function kpis(k){
  const el=document.getElementById('kpis');
  el.innerHTML=[
    ['total','Prospects',k.total],['brouillon','À envoyer',k.brouillon],
    ['envoye','Envoyés',k.envoye],['relances','Relances dues',k.relances],
    ['a_appeler','À appeler',k.a_appeler],['repondu','Ont répondu',k.repondu],
  ].map(([id,lib,v])=>`<div class="kpi"><b>${v}</b><span>${lib}</span></div>`).join('')
   + `<div class="kpi"><b>${k.quota}</b><span>quota / jour</span></div>`;
}

async function charger(){
  const r=await fetch('/api/prospects'); const j=await r.json();
  DATA=j.lignes; kpis(j.compteurs); rendre();
}

document.getElementById('filtres').innerHTML=
  FILTRES.map(([k,lib])=>`<button data-f="${k}"${k==='tous'?' class="on"':''}>${lib}</button>`).join('');
document.getElementById('filtres').addEventListener('click',e=>{
  const b=e.target.closest('button[data-f]'); if(!b)return;
  FILTRE=b.dataset.f;
  document.querySelectorAll('#filtres button').forEach(x=>x.classList.toggle('on',x===b));
  rendre();
});
document.getElementById('corps').addEventListener('click',async e=>{
  const b=e.target.closest('button[data-a]'); if(!b)return;
  const {a,p}=b.dataset;
  if(a==='stop'&&!confirm("Retirer définitivement cette adresse de tes fichiers ?"))return;
  b.disabled=true;
  const r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({place_id:p,action:a})});
  const j=await r.json();
  flash(j.message||'fait'); await charger();
});
charger();
</script>
"""


class _Handler(http.server.BaseHTTPRequestHandler):
    con: sqlite3.Connection = None   # injectés par servir()
    cfg: dict = None

    def log_message(self, *a):        # pas de bruit dans le terminal
        pass

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/prospects"):
            lignes = _lignes(self.con, self.cfg)
            self._json(200, {"lignes": lignes, "compteurs": _compteurs(lignes, self.cfg)})
        else:
            self._json(404, {"erreur": "inconnu"})

    def do_POST(self):
        if not self.path.startswith("/api/action"):
            return self._json(404, {"erreur": "inconnu"})
        taille = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(taille) or b"{}")
        except ValueError:
            return self._json(400, {"erreur": "JSON illisible"})
        pid, action = data.get("place_id"), data.get("action")
        if not pid:
            return self._json(400, {"erreur": "place_id manquant"})
        try:
            if action == "stop":
                n = _stopper(self.con, pid)
                msg = "Adresse retirée des fichiers." if n else "Pas d'adresse à retirer."
            else:
                n = _marquer(self.con, pid, action)
                msg = (f"Marqué « {STATUTS.get(action, action)} »." if n
                       else "Rien à changer sur ce prospect.")
        except ValueError as exc:
            return self._json(400, {"erreur": str(exc)})
        self._json(200, {"ok": True, "modifies": n, "message": msg})


def servir(chemin_db: str | Path | None, cfg: dict, port: int = 8770,
           ouvrir_navigateur: bool = True) -> None:
    """Sert le tableau de bord sur 127.0.0.1 — jamais sur toutes les interfaces."""
    con = store.ouvrir(chemin_db)
    con.row_factory = sqlite3.Row
    _Handler.con, _Handler.cfg = con, cfg

    lignes = _lignes(con, cfg)
    k = _compteurs(lignes, cfg)
    url = f"http://127.0.0.1:{port}/"
    print(f"Suivi de prospection : {url}")
    print(f"  {k['total']} prospect(s) · {k['brouillon']} à envoyer · "
          f"{k['a_appeler']} à appeler · {k['relances']} relance(s) due(s)")
    print("  Ctrl+C pour arrêter.")
    if ouvrir_navigateur:
        webbrowser.open(url)
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", port), _Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nSuivi arrêté.")
