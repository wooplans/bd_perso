from flask import Flask, request, jsonify, send_file
import fitz
import re, os, uuid, json, glob, base64, requests as http_requests

app = Flask(__name__)

BIBLIO_FOLDER = "./bibliotheque"
OUTPUT_FOLDER = "./output"
META_FILE     = "./bibliotheque/meta.json"
OPENAI_KEY    = os.environ.get("OPENAI_API_KEY", "")

os.makedirs(BIBLIO_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# ── Police ─────────────────────────────────────────────────────────────────
def _trouver_police():
    candidats = [
        "/usr/share/fonts/opentype/comic-neue/ComicNeue-Bold.otf",
        "/usr/share/fonts/truetype/comic-neue/ComicNeue-Bold.otf",
    ]
    for c in candidats:
        if os.path.exists(c): return c
    return None

POLICE_FALLBACK = _trouver_police()  # Comic Neue en secours

def extraire_police_pdf(chemin_pdf, nom_cible="MoreSugar"):
    """Extrait More Sugar directement depuis le PDF Canva."""
    try:
        doc = fitz.open(chemin_pdf)
        fonts = doc.get_page_fonts(0, full=True)
        for f in fonts:
            if nom_cible.lower() in f[3].lower():
                font_data = doc.extract_font(f[0])
                data = font_data[3]
                if data and len(data) > 1000:
                    chemin = f"/tmp/police_{uuid.uuid4().hex[:8]}.ttf"
                    with open(chemin, "wb") as out:
                        out.write(data)
                    return chemin
    except Exception:
        pass
    return POLICE_FALLBACK  # Fallback Comic Neue

# ── Méta bibliothèque ───────────────────────────────────────────────────────
def lire_meta():
    if not os.path.exists(META_FILE): return {}
    with open(META_FILE) as f: return json.load(f)

def ecrire_meta(meta):
    with open(META_FILE, "w") as f: json.dump(meta, f, ensure_ascii=False, indent=2)

# ── Personnalisation PDF ───────────────────────────────────────────────────
def adapter_casse(prenom_nouveau, texte, prenom_ancien):
    def remplacer(m):
        o = m.group(0)
        if o.isupper(): return prenom_nouveau.upper()
        elif o[0].isupper(): return prenom_nouveau.capitalize()
        return prenom_nouveau.lower()
    return re.compile(re.escape(prenom_ancien), re.IGNORECASE).sub(remplacer, texte)

def personnaliser_pdf_pages(chemin_pdf, prenom_ancien, prenom_nouveau):
    doc = fitz.open(chemin_pdf)
    # Extraire More Sugar depuis ce PDF (taille et style préservés)
    police = extraire_police_pdf(chemin_pdf, "MoreSugar")
    total = 0
    for page in doc:
        spans = []
        for b in page.get_text("dict")["blocks"]:
            if b["type"] != 0: continue
            for line in b["lines"]:
                for span in line["spans"]:
                    if prenom_ancien.upper() in span["text"].upper():
                        spans.append(span)
        for span in spans:
            bbox = fitz.Rect(span["bbox"])
            page.add_redact_annot(fitz.Rect(bbox.x0-1, bbox.y0-1, bbox.x1+1, bbox.y1+1), fill=(1,1,1))
        page.apply_redactions()
        for span in spans:
            texte_nouveau = adapter_casse(prenom_nouveau, span["text"], prenom_ancien)
            page.insert_text(span["origin"], texte_nouveau,
                             fontfile=police,       # More Sugar extraite du PDF
                             fontsize=span["size"], # taille exacte préservée
                             color=(0, 0, 0))
            total += 1
    return doc, total

# ── Génération couverture via GPT-Image ────────────────────────────────────
def generer_couverture(prompt_template, prenom, style_notes=""):
    """Appelle GPT-Image-1 pour générer une couverture personnalisée."""
    prompt = prompt_template.replace("{PRENOM}", prenom).replace("{prenom}", prenom)
    if style_notes:
        prompt += f"\n\nStyle : {style_notes}"

    response = http_requests.post(
        "https://api.openai.com/v1/images/generations",
        headers={
            "Authorization": f"Bearer {OPENAI_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "model": "gpt-image-1",
            "prompt": prompt,
            "n": 1,
            "size": "1024x1536",
            "quality": "medium",
            "output_format": "png"
        },
        timeout=120
    )
    if response.status_code != 200:
        raise Exception(f"OpenAI error {response.status_code}: {response.text[:200]}")

    data = response.json()
    # gpt-image-1 retourne base64
    img_b64 = data["data"][0].get("b64_json") or data["data"][0].get("url")

    if img_b64 and not img_b64.startswith("http"):
        img_bytes = base64.b64decode(img_b64)
        chemin = os.path.join(OUTPUT_FOLDER, f"couv_{uuid.uuid4().hex[:8]}.png")
        with open(chemin, "wb") as f:
            f.write(img_bytes)
        return chemin
    elif img_b64 and img_b64.startswith("http"):
        r = http_requests.get(img_b64, timeout=60)
        chemin = os.path.join(OUTPUT_FOLDER, f"couv_{uuid.uuid4().hex[:8]}.png")
        with open(chemin, "wb") as f:
            f.write(r.content)
        return chemin
    else:
        raise Exception("Réponse OpenAI inattendue")

# ── Assemblage PDF final ───────────────────────────────────────────────────
def assembler_pdf(chemin_couverture_png, doc_bd, prenom, compression):
    """Fusionne couverture PNG + pages BD en un seul PDF."""
    pdf_final = fitz.open()

    # ── Page de couverture ──
    img = fitz.open(chemin_couverture_png)
    img_pdf = fitz.open("pdf", img.convert_to_pdf())
    pdf_final.insert_pdf(img_pdf)

    # ── Pages BD ──
    pdf_final.insert_pdf(doc_bd)

    # ── Compression ──
    nom = f"BD_{prenom.capitalize()}_{uuid.uuid4().hex[:6]}.pdf"
    chemin = os.path.join(OUTPUT_FOLDER, nom)

    if compression == "forte":
        pdf_final.save(chemin, garbage=4, deflate=True, clean=True,
                       deflate_images=True, deflate_fonts=True)
    elif compression == "moyenne":
        pdf_final.save(chemin, garbage=3, deflate=True, clean=True)
    else:  # aucune
        pdf_final.save(chemin)

    return chemin

def assembler_pdf_sans_couverture(doc_bd, prenom, compression):
    """PDF sans couverture — juste les pages BD personnalisées."""
    pdf_final = fitz.open()
    pdf_final.insert_pdf(doc_bd)
    nom = f"BD_{prenom.capitalize()}_{uuid.uuid4().hex[:6]}.pdf"
    chemin = os.path.join(OUTPUT_FOLDER, nom)
    if compression == "forte":
        pdf_final.save(chemin, garbage=4, deflate=True, clean=True,
                       deflate_images=True, deflate_fonts=True)
    elif compression == "moyenne":
        pdf_final.save(chemin, garbage=3, deflate=True, clean=True)
    else:
        pdf_final.save(chemin)
    return chemin

# ══════════════════════════════════════════════════════════════════════════════
# HTML
# ══════════════════════════════════════════════════════════════════════════════
HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BD Personnalisée — EnfantProdige</title>
<link href="https://fonts.googleapis.com/css2?family=Fredoka+One&family=Nunito:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--orange:#FF6B35;--violet:#6C3CE1;--vert:#06D6A0;--gris:#F4F1FF;--texte:#1A1033;--doux:#6B5CA5;--rouge:#FF4D6D}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Nunito',sans-serif;background:var(--gris);min-height:100vh;color:var(--texte)}
body::before{content:'';position:fixed;inset:0;background:radial-gradient(circle at 15% 20%,rgba(108,60,225,.12),transparent 50%),radial-gradient(circle at 85% 80%,rgba(255,107,53,.10),transparent 50%);pointer-events:none;z-index:0}
.deco{position:fixed;font-size:2rem;opacity:.12;pointer-events:none;z-index:0;animation:flotte 6s ease-in-out infinite}
.deco:nth-child(1){top:8%;left:5%}.deco:nth-child(2){top:15%;right:8%;animation-delay:1s}.deco:nth-child(3){bottom:20%;left:8%;animation-delay:2s}.deco:nth-child(4){bottom:10%;right:5%;animation-delay:.5s}
@keyframes flotte{0%,100%{transform:translateY(0)}50%{transform:translateY(-12px)}}
.page{position:relative;z-index:1;max-width:580px;margin:0 auto;padding:28px 14px 60px;display:flex;flex-direction:column;align-items:center;gap:16px}
.header{text-align:center}
.badge{display:inline-block;background:var(--violet);color:#fff;font-size:11px;font-weight:800;letter-spacing:2px;text-transform:uppercase;padding:5px 14px;border-radius:20px;margin-bottom:10px}
.titre{font-family:'Fredoka One',cursive;font-size:2.2rem;line-height:1.1;margin-bottom:5px}
.titre span{color:var(--orange)}
.sous-titre{color:var(--doux);font-size:.9rem;font-weight:600}
.onglets{display:flex;gap:8px;width:100%}
.onglet{flex:1;padding:10px;border-radius:12px;border:2px solid rgba(108,60,225,.15);background:#fff;font-family:'Nunito',sans-serif;font-size:.85rem;font-weight:800;color:var(--doux);cursor:pointer;transition:all .2s;text-align:center}
.onglet.actif{background:var(--violet);color:#fff;border-color:var(--violet)}
.carte{width:100%;background:#fff;border-radius:20px;padding:22px;box-shadow:0 4px 0 rgba(108,60,225,.10),0 8px 28px rgba(108,60,225,.07);border:2px solid rgba(108,60,225,.06)}
.label{display:block;font-size:.72rem;font-weight:800;letter-spacing:1.5px;text-transform:uppercase;color:var(--doux);margin-bottom:6px}
.champ,.champ-nom,.select-bd,.textarea{width:100%;padding:11px 14px;border-radius:10px;border:2px solid rgba(108,60,225,.15);font-family:'Nunito',sans-serif;font-size:.9rem;font-weight:700;color:var(--texte);background:var(--gris);outline:none;margin-bottom:14px;transition:border-color .2s,box-shadow .2s}
.champ:focus,.champ-nom:focus,.select-bd:focus,.textarea:focus{border-color:var(--violet);box-shadow:0 0 0 3px rgba(108,60,225,.10);background:#fff}
.champ::placeholder,.champ-nom::placeholder,.textarea::placeholder{color:rgba(107,92,165,.4);font-weight:600}
.textarea{resize:vertical;min-height:90px;line-height:1.5}
.select-bd{cursor:pointer;appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%236B5CA5' stroke-width='2' fill='none' stroke-linecap='round'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 12px center;background-color:var(--gris)}
.select-bd:focus{background-color:#fff}
.apercu{display:flex;align-items:center;gap:10px;padding:10px 13px;background:var(--gris);border-radius:10px;margin-bottom:14px}
.ap-avant{flex:1;text-align:center;font-size:.88rem;font-weight:800;color:var(--doux);opacity:.5}
.fleche{font-size:1.2rem;color:var(--orange);flex-shrink:0}
.ap-apres{flex:1;text-align:center;font-size:.95rem;font-weight:800;color:var(--violet);min-height:1.2em}

/* Options couverture */
.section-couv{background:rgba(108,60,225,.04);border:2px solid rgba(108,60,225,.10);border-radius:12px;padding:14px;margin-bottom:14px}
.toggle-row{display:flex;align-items:center;justify-content:space-between;margin-bottom:0}
.toggle-row.open{margin-bottom:12px}
.toggle-label{font-size:.85rem;font-weight:800;color:var(--texte)}
.toggle-track{position:relative;width:44px;height:24px;background:rgba(108,60,225,.2);border-radius:12px;transition:background .2s;cursor:pointer;flex-shrink:0}
.toggle-track:has(input:checked){background:var(--violet)}
.toggle-track input{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
.toggle-knob{position:absolute;top:3px;left:3px;width:18px;height:18px;background:#fff;border-radius:50%;transition:transform .2s;pointer-events:none}
.toggle-track:has(input:checked) .toggle-knob{transform:translateX(20px)}
.couv-options{display:none}
.couv-options.visible{display:block}
.couv-upload{display:none}
.couv-upload.visible{display:block}
.zone-couv{border:2.5px dashed rgba(255,107,53,.3);border-radius:12px;padding:18px 14px;text-align:center;cursor:pointer;transition:all .2s;background:rgba(255,107,53,.03);position:relative;margin-top:12px}
.zone-couv:hover,.zone-couv.survol{border-color:var(--orange);background:rgba(255,107,53,.06)}
.zone-couv.ok{border-color:var(--vert);background:rgba(6,214,160,.05)}
.zone-couv input{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
.couv-preview{display:none;margin-top:10px;width:100%;max-height:160px;object-fit:contain;border-radius:8px;border:2px solid rgba(6,214,160,.3)}

/* Compression */
.comp-row{display:flex;gap:8px;margin-bottom:14px}
.comp-btn{flex:1;padding:9px 6px;border-radius:9px;border:2px solid rgba(108,60,225,.15);background:var(--gris);font-family:'Nunito',sans-serif;font-size:.78rem;font-weight:800;color:var(--doux);cursor:pointer;transition:all .2s;text-align:center}
.comp-btn.actif{background:var(--violet);color:#fff;border-color:var(--violet)}

/* Boutons */
.btn{width:100%;padding:14px;border-radius:13px;border:none;background:linear-gradient(135deg,var(--violet),var(--orange));color:#fff;font-family:'Fredoka One',cursive;font-size:1.15rem;cursor:pointer;transition:all .2s;box-shadow:0 4px 0 rgba(108,60,225,.30)}
.btn:hover:not(:disabled){transform:translateY(-2px);box-shadow:0 6px 0 rgba(108,60,225,.30)}
.btn:active:not(:disabled){transform:translateY(2px);box-shadow:0 2px 0 rgba(108,60,225,.30)}
.btn:disabled{opacity:.6;cursor:not-allowed}
.btn-sm{width:100%;padding:12px;border-radius:11px;border:none;background:var(--violet);color:#fff;font-family:'Fredoka One',cursive;font-size:1.05rem;cursor:pointer;transition:all .2s;box-shadow:0 3px 0 rgba(108,60,225,.25)}
.btn-sm:hover{transform:translateY(-1px)}

/* Upload zone */
.zone-upload{border:2.5px dashed rgba(108,60,225,.25);border-radius:13px;padding:22px 14px;text-align:center;cursor:pointer;transition:all .2s;background:var(--gris);position:relative;margin-bottom:14px}
.zone-upload:hover,.zone-upload.survol{border-color:var(--violet);background:rgba(108,60,225,.04)}
.zone-upload.ok{border-color:var(--vert);background:rgba(6,214,160,.05)}
.zone-upload input{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
.zone-upload .icone{font-size:2rem;margin-bottom:6px;display:block}
.zone-upload .lbl{font-size:.85rem;font-weight:700;color:var(--violet);margin-bottom:2px}
.zone-upload .sub{font-size:.75rem;color:var(--doux)}
.nom-fichier{display:none;margin-top:6px;font-size:.78rem;font-weight:700;color:var(--vert)}

/* Liste BDs */
.liste-bds{display:flex;flex-direction:column;gap:8px;margin-bottom:14px;max-height:200px;overflow-y:auto}
.bd-item{display:flex;align-items:center;justify-content:space-between;padding:11px 14px;border-radius:11px;background:var(--gris);border:2px solid transparent;cursor:pointer;transition:all .2s}
.bd-item:hover{border-color:rgba(108,60,225,.2)}
.bd-nom{font-weight:800;font-size:.85rem;color:var(--texte)}
.bd-prenom{font-size:.72rem;color:var(--doux);margin-top:1px}
.bd-couv{font-size:.68rem;color:var(--vert);margin-top:1px}
.bd-suppr{background:none;border:none;color:rgba(255,77,109,.45);font-size:1rem;cursor:pointer;padding:4px;border-radius:6px;transition:color .2s}
.bd-suppr:hover{color:var(--rouge)}
.liste-vide{text-align:center;padding:20px;color:var(--doux);font-size:.85rem;font-weight:600}

/* Loader */
.loader{display:none;text-align:center;padding:14px}
.loader.actif{display:block}
.points{display:inline-flex;gap:5px;margin-bottom:7px}
.points span{width:8px;height:8px;border-radius:50%;background:var(--violet);animation:rebond 1s ease-in-out infinite}
.points span:nth-child(2){animation-delay:.15s;background:var(--orange)}
.points span:nth-child(3){animation-delay:.3s;background:var(--vert)}
@keyframes rebond{0%,80%,100%{transform:scale(.7);opacity:.5}40%{transform:scale(1.2);opacity:1}}
.loader-steps{list-style:none;text-align:left;display:inline-block;margin-top:4px}
.loader-steps li{font-size:.8rem;color:var(--doux);font-weight:700;padding:2px 0;opacity:.4;transition:opacity .3s}
.loader-steps li.en-cours{opacity:1;color:var(--violet)}
.loader-steps li.fait{opacity:.7}
.loader-steps li.fait::before{content:"✓ ";color:var(--vert)}

/* Résultat */
.resultat{display:none;text-align:center;padding:20px;border-radius:15px;background:linear-gradient(135deg,rgba(6,214,160,.07),rgba(108,60,225,.05));border:2px solid rgba(6,214,160,.22)}
.resultat.actif{display:block;animation:pop .35s ease}
@keyframes pop{from{opacity:0;transform:scale(.95) translateY(8px)}to{opacity:1;transform:scale(1) translateY(0)}}
.res-emoji{font-size:2.6rem;margin-bottom:5px}
.res-titre{font-family:'Fredoka One',cursive;font-size:1.3rem;color:var(--texte);margin-bottom:3px}
.res-info{font-size:.8rem;color:var(--doux);margin-bottom:16px}
.res-taille{display:inline-block;background:rgba(108,60,225,.08);color:var(--violet);font-size:.75rem;font-weight:800;padding:3px 10px;border-radius:8px;margin-bottom:14px}
.btn-dl{display:inline-flex;align-items:center;gap:7px;padding:12px 24px;border-radius:11px;background:var(--vert);color:#fff;font-family:'Fredoka One',cursive;font-size:1rem;text-decoration:none;transition:all .2s;box-shadow:0 3px 0 rgba(6,214,160,.3)}
.btn-dl:hover{transform:translateY(-2px)}
.btn-nouveau{display:block;margin-top:10px;background:none;border:none;color:var(--doux);font-family:'Nunito',sans-serif;font-size:.8rem;font-weight:700;cursor:pointer;text-decoration:underline}

/* Messages */
.msg{display:none;padding:11px 14px;border-radius:9px;font-weight:700;font-size:.83rem;text-align:center;margin-top:8px}
.msg.actif{display:block}
.msg.err{background:rgba(255,77,109,.07);border:2px solid rgba(255,77,109,.22);color:var(--rouge)}
.msg.ok{background:rgba(6,214,160,.07);border:2px solid rgba(6,214,160,.22);color:#059669}

.sep{height:1px;background:rgba(108,60,225,.08);margin:14px 0}
.footer{text-align:center;font-size:.72rem;color:var(--doux);opacity:.5;font-weight:600}
</style>
</head>
<body>
<div class="deco">⭐</div><div class="deco">📚</div><div class="deco">🚀</div><div class="deco">💡</div>

<div class="page">
  <div class="header">
    <div class="badge">EnfantProdige</div>
    <h1 class="titre">BD <span>Personnalisée</span></h1>
    <p class="sous-titre">Couverture IA + BD personnalisée = PDF prêt à envoyer ✨</p>
  </div>

  <!-- Onglets -->
  <div class="onglets">
    <button class="onglet actif" onclick="changerOnglet('perso',this)">🎨 Personnaliser</button>
    <button class="onglet" onclick="changerOnglet('biblio',this)">📚 Bibliothèque</button>
  </div>

  <!-- ═══ ONGLET PERSONNALISER ═══ -->
  <div class="carte" id="carte-perso">

    <label class="label">Choisir la BD</label>
    <select class="select-bd" id="select-bd" onchange="bdSelectionnee()">
      <option value="">— Sélectionner une BD —</option>
    </select>

    <div class="apercu">
      <div class="ap-avant" id="ap-avant">WILLIAM</div>
      <div class="fleche">→</div>
      <div class="ap-apres" id="ap-apres">…</div>
    </div>

    <label class="label">Prénom de l'enfant</label>
    <input type="text" class="champ" id="prenom-nouveau" placeholder="Ex : AMINATA"
           oninput="majApercu()" autocomplete="off">

    <!-- Couverture -->
    <div class="section-couv">
      <div class="toggle-row" id="toggle-row">
        <span class="toggle-label">🎨 Générer la couverture avec IA</span>
        <label class="toggle-track">
          <input type="checkbox" id="toggle-couv" onchange="toggleCouv()">
          <span class="toggle-knob"></span>
        </label>
      </div>

      <!-- Mode IA : prompt -->
      <div class="couv-options" id="couv-options">
        <div class="sep"></div>
        <label class="label">Prompt de la couverture</label>
        <textarea class="textarea" id="prompt-couv"
          placeholder="Ex : Couverture BD enfants africains. Héros = {PRENOM}, 8 ans, lunettes, uniforme. Style manga noir et blanc. Titre : « {PRENOM} sauve son école »."></textarea>
        <div style="font-size:.72rem;color:var(--doux);margin-top:-10px;margin-bottom:14px">
          💡 Utilise <strong>{PRENOM}</strong> dans le prompt — remplacé automatiquement
        </div>
      </div>

      <!-- Mode Manuel : upload image -->
      <div class="couv-upload" id="couv-upload">
        <div class="sep"></div>
        <label class="label">Couverture personnalisée</label>
        <div class="zone-couv" id="zone-couv">
          <input type="file" id="input-couv" accept="image/*,.pdf" onchange="previewCouv()">
          <span style="font-size:1.8rem;display:block;margin-bottom:6px">🖼️</span>
          <div style="font-size:.85rem;font-weight:700;color:var(--orange);margin-bottom:2px">Glisse la couverture ici</div>
          <div style="font-size:.75rem;color:var(--doux)">PNG, JPG, WEBP ou PDF</div>
        </div>
        <img class="couv-preview" id="couv-preview" src="" alt="Aperçu couverture">
        <div id="couv-nom" style="display:none;font-size:.78rem;font-weight:700;color:var(--vert);margin-top:6px;text-align:center"></div>
      </div>
    </div>

    <!-- Compression -->
    <label class="label">Compression du PDF final</label>
    <div class="comp-row">
      <button class="comp-btn" onclick="setComp('aucune',this)">📄 Aucune</button>
      <button class="comp-btn actif" onclick="setComp('moyenne',this)">⚖️ Moyenne</button>
      <button class="comp-btn" onclick="setComp('forte',this)">🗜️ Forte</button>
    </div>

    <button class="btn" id="btn-gen" onclick="generer()">🚀 Générer le PDF final</button>

    <div class="loader" id="loader">
      <div class="points"><span></span><span></span><span></span></div>
      <ul class="loader-steps" id="loader-steps">
        <li id="step-bd">Personnalisation de la BD…</li>
        <li id="step-couv">Génération de la couverture IA…</li>
        <li id="step-assemble">Assemblage du PDF…</li>
        <li id="step-compress">Compression…</li>
      </ul>
    </div>
    <div class="msg err" id="err-perso"></div>

    <!-- Résultat -->
    <div class="resultat" id="resultat">
      <div class="res-emoji">🎉</div>
      <div class="res-titre" id="res-titre">PDF prêt !</div>
      <div class="res-info" id="res-info"></div>
      <div class="res-taille" id="res-taille"></div>
      <br>
      <a href="#" class="btn-dl" id="btn-dl" download>⬇️ Télécharger le PDF</a>
      <button class="btn-nouveau" onclick="nouveau()">Personnaliser une autre BD</button>
    </div>
  </div>

  <!-- ═══ ONGLET BIBLIOTHÈQUE ═══ -->
  <div class="carte" style="display:none" id="carte-biblio">

    <div class="zone-upload" id="zone-up">
      <input type="file" id="input-pdf" accept=".pdf">
      <span class="icone">📄</span>
      <div class="lbl">Ajouter une BD à la bibliothèque</div>
      <div class="sub">Glisse le PDF ici ou clique</div>
      <div class="nom-fichier" id="nom-fich"></div>
    </div>

    <label class="label">Nom de la BD</label>
    <input type="text" class="champ-nom" id="nom-bd" placeholder="Ex : Académie des Génies — Tome 1">

    <label class="label">Prénom placeholder dans le PDF</label>
    <input type="text" class="champ-nom" id="prenom-bd" placeholder="Ex : WILLIAM">

    <label class="label">Prompt couverture (optionnel)</label>
    <textarea class="textarea" id="prompt-biblio"
      placeholder="Ex : Couverture BD enfants africains. Héros = {PRENOM}, 8 ans, lunettes, uniforme. Style manga noir et blanc. Titre : « {PRENOM} sauve son école »."></textarea>

    <button class="btn-sm" onclick="uploadBD()">➕ Ajouter à la bibliothèque</button>
    <div class="msg" id="msg-upload"></div>

    <div class="sep"></div>
    <label class="label">BDs disponibles</label>
    <div class="liste-bds" id="liste-bds">
      <div class="liste-vide">Aucune BD pour l'instant</div>
    </div>
  </div>

  <div class="footer">EnfantProdige · Académie des Génies · Yaoundé</div>
</div>

<script>
let compression = 'moyenne';

// ── Onglets ──────────────────────────────────────────────────────────────────
function changerOnglet(id, btn) {
  document.querySelectorAll('.onglet').forEach(b => b.classList.remove('actif'));
  btn.classList.add('actif');
  document.getElementById('carte-perso').style.display  = id === 'perso'  ? 'block' : 'none';
  document.getElementById('carte-biblio').style.display = id === 'biblio' ? 'block' : 'none';
  if (id === 'biblio') chargerListe();
}

// ── Toggle couverture ─────────────────────────────────────────────────────────
function toggleCouv() {
  const on   = document.getElementById('toggle-couv').checked;
  const opts = document.getElementById('couv-options');
  const up   = document.getElementById('couv-upload');
  const row  = document.getElementById('toggle-row');
  opts.classList.toggle('visible', on);   // IA actif → prompt visible
  up.classList.toggle('visible', !on);    // IA inactif → upload visible
  row.classList.toggle('open', true);
}

function previewCouv() {
  const input = document.getElementById('input-couv');
  const f = input.files[0];
  if (!f) return;
  document.getElementById('couv-nom').style.display = 'block';
  document.getElementById('couv-nom').textContent = '✓ ' + f.name;
  document.getElementById('zone-couv').classList.add('ok');
  // Aperçu image seulement (pas PDF)
  if (f.type.startsWith('image/')) {
    const url = URL.createObjectURL(f);
    const prev = document.getElementById('couv-preview');
    prev.src = url; prev.style.display = 'block';
  }
}

// ── Compression ───────────────────────────────────────────────────────────────
function setComp(val, btn) {
  compression = val;
  document.querySelectorAll('.comp-btn').forEach(b => b.classList.remove('actif'));
  btn.classList.add('actif');
}

// ── Upload bibliothèque ───────────────────────────────────────────────────────
const inputPdf = document.getElementById('input-pdf');
const zoneUp   = document.getElementById('zone-up');
const nomFich  = document.getElementById('nom-fich');

inputPdf.addEventListener('change', () => {
  const f = inputPdf.files[0];
  if (f) {
    zoneUp.classList.add('ok'); nomFich.style.display = 'block';
    nomFich.textContent = '✓ ' + f.name;
    if (!document.getElementById('nom-bd').value)
      document.getElementById('nom-bd').value = f.name.replace('.pdf','');
  }
});
zoneUp.addEventListener('dragover', e => { e.preventDefault(); zoneUp.classList.add('survol'); });
zoneUp.addEventListener('dragleave', () => zoneUp.classList.remove('survol'));
zoneUp.addEventListener('drop', e => {
  e.preventDefault(); zoneUp.classList.remove('survol');
  const f = e.dataTransfer.files[0];
  if (f && f.name.endsWith('.pdf')) {
    const dt = new DataTransfer(); dt.items.add(f); inputPdf.files = dt.files;
    zoneUp.classList.add('ok'); nomFich.style.display = 'block';
    nomFich.textContent = '✓ ' + f.name;
    if (!document.getElementById('nom-bd').value)
      document.getElementById('nom-bd').value = f.name.replace('.pdf','');
  }
});

async function uploadBD() {
  const f = inputPdf.files[0];
  const nom    = document.getElementById('nom-bd').value.trim();
  const prenom = document.getElementById('prenom-bd').value.trim();
  const prompt = document.getElementById('prompt-biblio').value.trim();
  const msgEl  = document.getElementById('msg-upload');
  msgEl.className = 'msg';

  if (!f)     { affMsg(msgEl,'Choisis un fichier PDF.','err'); return; }
  if (!nom)   { affMsg(msgEl,'Donne un nom à cette BD.','err'); return; }
  if (!prenom){ affMsg(msgEl,'Indique le prénom placeholder.','err'); return; }

  const fd = new FormData();
  fd.append('pdf', f); fd.append('nom', nom);
  fd.append('prenom', prenom); fd.append('prompt_couv', prompt);

  const res  = await fetch('/ajouter-bd', { method:'POST', body:fd });
  const data = await res.json();
  if (data.erreur) { affMsg(msgEl, data.erreur, 'err'); return; }
  affMsg(msgEl, '✓ BD ajoutée !', 'ok');
  inputPdf.value = ''; zoneUp.classList.remove('ok');
  nomFich.style.display = 'none';
  document.getElementById('nom-bd').value = '';
  document.getElementById('prenom-bd').value = '';
  document.getElementById('prompt-biblio').value = '';
  chargerListe(); chargerSelectBD();
}

// ── Liste bibliothèque ────────────────────────────────────────────────────────
async function chargerListe() {
  const res  = await fetch('/liste-bds');
  const data = await res.json();
  const el   = document.getElementById('liste-bds');
  if (!data.bds || !data.bds.length) {
    el.innerHTML = '<div class="liste-vide">Aucune BD pour l\'instant</div>'; return;
  }
  el.innerHTML = data.bds.map(bd => `
    <div class="bd-item">
      <div>
        <div class="bd-nom">📖 ${bd.nom}</div>
        <div class="bd-prenom">Héros : ${bd.prenom}</div>
        ${bd.prompt_couv ? '<div class="bd-couv">🎨 Prompt couverture enregistré</div>' : ''}
      </div>
      <button class="bd-suppr" onclick="supprimerBD('${bd.id}')">🗑</button>
    </div>`).join('');
}

async function supprimerBD(id) {
  if (!confirm('Supprimer cette BD ?')) return;
  await fetch('/supprimer-bd/'+id, { method:'DELETE' });
  chargerListe(); chargerSelectBD();
}

// ── Select personnaliser ──────────────────────────────────────────────────────
async function chargerSelectBD() {
  const res  = await fetch('/liste-bds');
  const data = await res.json();
  const sel  = document.getElementById('select-bd');
  const val  = sel.value;
  sel.innerHTML = '<option value="">— Sélectionner une BD —</option>';
  (data.bds || []).forEach(bd => {
    const opt = document.createElement('option');
    opt.value = bd.id; opt.textContent = bd.nom;
    opt.dataset.prenom = bd.prenom;
    opt.dataset.prompt = bd.prompt_couv || '';
    sel.appendChild(opt);
  });
  if (val) sel.value = val;
  bdSelectionnee();
}

function bdSelectionnee() {
  const sel = document.getElementById('select-bd');
  const opt = sel.options[sel.selectedIndex];
  document.getElementById('ap-avant').textContent = (opt?.dataset?.prenom || 'WILLIAM').toUpperCase();
  // Pré-remplir le prompt si dispo
  if (opt?.dataset?.prompt)
    document.getElementById('prompt-couv').value = opt.dataset.prompt;
  majApercu();
}

function majApercu() {
  const v = document.getElementById('prenom-nouveau').value;
  document.getElementById('ap-apres').textContent = v ? v.toUpperCase() : '…';
}

// ── Étapes loader ────────────────────────────────────────────────────────────
function setStep(id) {
  ['step-bd','step-couv','step-assemble','step-compress'].forEach(s => {
    document.getElementById(s).className = '';
  });
  if (id) document.getElementById(id).className = 'en-cours';
}
function doneStep(id) {
  if (id) document.getElementById(id).className = 'fait';
}

// ── Génération ────────────────────────────────────────────────────────────────
async function generer() {
  const bdId    = document.getElementById('select-bd').value;
  const nouveau = document.getElementById('prenom-nouveau').value.trim();
  const avecCouv = document.getElementById('toggle-couv').checked;
  const prompt   = document.getElementById('prompt-couv').value.trim();
  const inputCouv = document.getElementById('input-couv');
  const errEl    = document.getElementById('err-perso');
  errEl.className = 'msg err';

  if (!bdId)    { affMsg(errEl,'Sélectionne une BD.','err'); return; }
  if (!nouveau) { affMsg(errEl,"Entre le prénom de l'enfant.",'err'); return; }
  if (avecCouv && !prompt) { affMsg(errEl,'Entre un prompt pour la couverture.','err'); return; }

  document.getElementById('btn-gen').disabled = true;
  document.getElementById('loader').classList.add('actif');
  document.getElementById('resultat').classList.remove('actif');

  // Afficher les étapes pertinentes
  document.getElementById('step-couv').style.display = avecCouv ? 'list-item' : 'none';
  document.getElementById('step-compress').style.display = compression !== 'aucune' ? 'list-item' : 'none';
  setStep('step-bd');

  try {
    // Préparer la requête (FormData pour gérer l'image de couverture)
    const fd = new FormData();
    fd.append('bd_id', bdId);
    fd.append('prenom_nouveau', nouveau);
    fd.append('avec_couverture', avecCouv ? '1' : '0');
    fd.append('prompt_couverture', prompt);
    fd.append('compression', compression);

    // Couverture image manuelle
    const inputCouv = document.getElementById('input-couv');
    if (!avecCouv && inputCouv.files[0]) {
      fd.append('couverture_image', inputCouv.files[0]);
    }

    const res = await fetch('/generer', { method: 'POST', body: fd });

    // Simuler progression visuelle
    if (avecCouv) {
      setTimeout(() => { doneStep('step-bd'); setStep('step-couv'); }, 800);
      setTimeout(() => { doneStep('step-couv'); setStep('step-assemble'); }, 8000);
      setTimeout(() => { doneStep('step-assemble'); setStep('step-compress'); }, 9500);
    } else {
      setTimeout(() => { doneStep('step-bd'); setStep('step-assemble'); }, 800);
      setTimeout(() => { doneStep('step-assemble'); setStep('step-compress'); }, 1500);
    }

    const data = await res.json();
    if (!res.ok || data.erreur) { affMsg(errEl, data.erreur || 'Erreur.', 'err'); return; }

    // Succès
    ['step-bd','step-couv','step-assemble','step-compress'].forEach(s => {
      const el = document.getElementById(s);
      if (el.style.display !== 'none') el.className = 'fait';
    });

    const prenom_cap = nouveau.charAt(0).toUpperCase() + nouveau.slice(1).toLowerCase();
    document.getElementById('res-titre').textContent = `PDF de ${prenom_cap} prêt ! 🎉`;
    document.getElementById('res-info').textContent =
      `${data.pages} pages · ${data.avec_couverture ? 'Couverture IA incluse · ' : ''}Compression ${compression}`;
    document.getElementById('res-taille').textContent = `📦 ${data.taille_mo} Mo`;
    document.getElementById('btn-dl').href = '/telecharger/' + data.fichier;
    document.getElementById('btn-dl').download = `BD_${prenom_cap}.pdf`;
    document.getElementById('resultat').classList.add('actif');

  } catch(e) {
    affMsg(errEl, 'Erreur de connexion : ' + e.message, 'err');
  } finally {
    document.getElementById('btn-gen').disabled = false;
    document.getElementById('loader').classList.remove('actif');
    setStep(null);
  }
}

function nouveau() {
  document.getElementById('resultat').classList.remove('actif');
  document.getElementById('prenom-nouveau').value = '';
  document.getElementById('ap-apres').textContent = '…';
}

function affMsg(el, txt, cls) {
  el.textContent = txt; el.className = 'msg ' + cls;
}

// Init
chargerSelectBD();
// Couverture upload visible par défaut (toggle IA = OFF)
document.getElementById('couv-upload').classList.add('visible');
document.getElementById('toggle-row').classList.add('open');
</script>
</body>
</html>"""

# ══════════════════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return HTML

@app.route("/ajouter-bd", methods=["POST"])
def ajouter_bd():
    fichier     = request.files.get("pdf")
    nom         = request.form.get("nom","").strip()
    prenom      = request.form.get("prenom","").strip().upper()
    prompt_couv = request.form.get("prompt_couv","").strip()

    if not fichier or not nom or not prenom:
        return jsonify({"erreur":"Données manquantes"}), 400

    bd_id = uuid.uuid4().hex[:10]
    chemin = os.path.join(BIBLIO_FOLDER, f"{bd_id}.pdf")
    fichier.save(chemin)

    meta = lire_meta()
    meta[bd_id] = {"id":bd_id,"nom":nom,"prenom":prenom,
                   "fichier":f"{bd_id}.pdf","prompt_couv":prompt_couv}
    ecrire_meta(meta)
    return jsonify({"succes":True,"id":bd_id})

@app.route("/liste-bds")
def liste_bds():
    return jsonify({"bds": list(lire_meta().values())})

@app.route("/supprimer-bd/<bd_id>", methods=["DELETE"])
def supprimer_bd(bd_id):
    meta = lire_meta()
    if bd_id not in meta: return jsonify({"erreur":"BD introuvable"}), 404
    chemin = os.path.join(BIBLIO_FOLDER, meta[bd_id]["fichier"])
    if os.path.exists(chemin): os.remove(chemin)
    del meta[bd_id]
    ecrire_meta(meta)
    return jsonify({"succes":True})

@app.route("/generer", methods=["POST"])
def generer():
    # Accepte FormData (pour l'image de couverture)
    bd_id          = request.form.get("bd_id","")
    prenom_nouveau = request.form.get("prenom_nouveau","").strip()
    avec_couv      = request.form.get("avec_couverture","0") == "1"
    prompt_couv    = request.form.get("prompt_couverture","").strip()
    compression    = request.form.get("compression","moyenne")
    couv_image     = request.files.get("couverture_image")

    meta = lire_meta()
    if bd_id not in meta:
        return jsonify({"erreur":"BD introuvable"}), 404

    bd = meta[bd_id]
    chemin_pdf    = os.path.join(BIBLIO_FOLDER, bd["fichier"])
    prenom_ancien = bd["prenom"]

    # ── 1. Personnaliser la BD ──
    try:
        doc_bd, nb = personnaliser_pdf_pages(chemin_pdf, prenom_ancien, prenom_nouveau)
    except Exception as e:
        return jsonify({"erreur": f"Erreur BD : {str(e)}"}), 500

    if nb == 0:
        return jsonify({"erreur": f"'{prenom_ancien}' introuvable dans le PDF"}), 404

    # ── 2. Couverture ──
    chemin_couv = None
    if avec_couv:
        # Mode IA : génération GPT-Image
        prompt = prompt_couv or bd.get("prompt_couv","")
        if not prompt:
            return jsonify({"erreur":"Prompt couverture manquant"}), 400
        if not OPENAI_KEY:
            return jsonify({"erreur":"Clé OpenAI non configurée (variable OPENAI_API_KEY)"}), 500
        try:
            chemin_couv = generer_couverture(prompt, prenom_nouveau)
        except Exception as e:
            return jsonify({"erreur": f"Erreur couverture IA : {str(e)}"}), 500
    elif couv_image:
        # Mode Manuel : image uploadée → convertie en PNG temporaire
        try:
            import tempfile
            from PIL import Image as PILImage
            ext = couv_image.filename.rsplit(".",1)[-1].lower()
            chemin_tmp = f"/tmp/couv_upload_{uuid.uuid4().hex[:8]}.{ext}"
            couv_image.save(chemin_tmp)
            # Convertir en PNG si besoin (PyMuPDF accepte PNG/JPEG/PDF)
            if ext in ("jpg","jpeg","png","webp"):
                img = PILImage.open(chemin_tmp).convert("RGB")
                chemin_couv = f"/tmp/couv_{uuid.uuid4().hex[:8]}.png"
                img.save(chemin_couv, "PNG")
                os.remove(chemin_tmp)
            elif ext == "pdf":
                chemin_couv = chemin_tmp  # déjà un PDF, géré séparément
            else:
                return jsonify({"erreur":"Format couverture non supporté"}), 400
        except Exception as e:
            return jsonify({"erreur": f"Erreur couverture image : {str(e)}"}), 500

    # ── 3. Assembler ──
    try:
        if chemin_couv and chemin_couv.endswith(".pdf"):
            # Couverture déjà en PDF → insérer directement
            doc_couv = fitz.open(chemin_couv)
            pdf_final = fitz.open()
            pdf_final.insert_pdf(doc_couv)
            pdf_final.insert_pdf(doc_bd)
            nom = f"BD_{prenom_nouveau.capitalize()}_{uuid.uuid4().hex[:6]}.pdf"
            chemin_final = os.path.join(OUTPUT_FOLDER, nom)
            if compression == "forte":
                pdf_final.save(chemin_final, garbage=4, deflate=True, clean=True, deflate_images=True, deflate_fonts=True)
            elif compression == "moyenne":
                pdf_final.save(chemin_final, garbage=3, deflate=True, clean=True)
            else:
                pdf_final.save(chemin_final)
        elif chemin_couv:
            chemin_final = assembler_pdf(chemin_couv, doc_bd, prenom_nouveau, compression)
        else:
            chemin_final = assembler_pdf_sans_couverture(doc_bd, prenom_nouveau, compression)
    except Exception as e:
        return jsonify({"erreur": f"Erreur assemblage : {str(e)}"}), 500
    finally:
        if chemin_couv and os.path.exists(chemin_couv):
            os.remove(chemin_couv)

    taille_mo = round(os.path.getsize(chemin_final) / (1024*1024), 1)
    doc_final = fitz.open(chemin_final)
    nb_pages  = len(doc_final)

    return jsonify({
        "succes": True,
        "fichier": os.path.basename(chemin_final),
        "taille_mo": taille_mo,
        "pages": nb_pages,
        "avec_couverture": avec_couv
    })

@app.route("/telecharger/<nom>")
def telecharger(nom):
    chemin = os.path.join(OUTPUT_FOLDER, nom)
    if not os.path.exists(chemin): return "Fichier introuvable", 404
    return send_file(chemin, as_attachment=True, download_name=nom)

if __name__ == "__main__":
    app.run(debug=True, port=5000)
