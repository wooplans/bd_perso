from flask import Flask, request, jsonify, send_file
import fitz
import re, os, uuid, json, glob

app = Flask(__name__)

BIBLIO_FOLDER = "./bibliotheque"
OUTPUT_FOLDER = "./output"
META_FILE     = "./bibliotheque/meta.json"

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

# Dossier fonts dans le repo (polices complètes uploadées)
FONTS_FOLDER = "./fonts"
os.makedirs(FONTS_FOLDER, exist_ok=True)

def trouver_police_repo(nom_span):
    """
    Cherche une police complète dans ./fonts/ ET à la racine du repo.
    Correspondance souple sur le nom de fichier.
    Ex: span "ComicSansMS" → trouve comic.ttf
    """
    nom_lower = nom_span.lower().replace("-","").replace(" ","").replace("_","")

    # Dossiers à chercher : racine du repo + sous-dossier fonts/
    dossiers = [".", FONTS_FOLDER]

    try:
        for dossier in dossiers:
            if not os.path.exists(dossier):
                continue
            for f in os.listdir(dossier):
                if f.lower().endswith((".ttf",".otf")):
                    f_lower = f.lower().replace("-","").replace(" ","").replace("_","")
                    nom_f = f_lower.replace(".ttf","").replace(".otf","")
                    if nom_lower in nom_f or nom_f in nom_lower:
                        return os.path.join(dossier, f)
    except Exception:
        pass
    return None

def extraire_polices_pdf(doc):
    """
    Extrait les polices embarquées du PDF.
    ⚠️ Canva sous-ensemblise les polices (glyphes limités).
    Retourne un dict : { nom_normalisé → chemin_tmp }
    """
    cache = {}
    try:
        fonts = doc.get_page_fonts(0, full=True)
        for f in fonts:
            xref     = f[0]
            nom_full = f[3]
            nom_clean = nom_full.split("+")[-1].lower()
            if nom_clean in cache:
                continue
            font_data = doc.extract_font(xref)
            data = font_data[3]
            if data and len(data) > 500:
                chemin = f"/tmp/police_{nom_clean}_{uuid.uuid4().hex[:6]}.ttf"
                with open(chemin, "wb") as out:
                    out.write(data)
                cache[nom_clean] = chemin
    except Exception:
        pass
    return cache

def police_pour_span(span, cache_polices):
    """
    Priorité :
    1. Police complète dans ./fonts/ du repo  ← idéal, glyphes complets
    2. Police extraite du PDF                 ← glyphes limités (subset Canva)
    3. Fallback Comic Neue
    """
    nom_span = span["font"].lower()

    # 1. Chercher dans ./fonts/ (police complète)
    police_repo = trouver_police_repo(nom_span)
    if police_repo:
        return police_repo

    # 2. Police extraite du PDF (attention aux glyphes manquants)
    if nom_span in cache_polices:
        return cache_polices[nom_span]
    for nom_cache, chemin in cache_polices.items():
        if nom_span in nom_cache or nom_cache in nom_span:
            return chemin

    # 3. Fallback
    return POLICE_FALLBACK

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

def est_bloc_centre(bloc, page_largeur=595.0, tol_multi=3.0, tol_page=5.0):
    """
    Détecte si un bloc Canva est centré. Deux cas :
    1. Plusieurs lignes → toutes ont le même centre X (± tol_multi)
    2. Une seule ligne  → son centre X ≈ centre de la page (± tol_page)
    """
    centres = []
    for line in bloc["lines"]:
        for span in line["spans"]:
            if span["text"].strip():
                bbox = span["bbox"]
                centres.append((bbox[0] + bbox[2]) / 2)

    if not centres:
        return False, 0

    # ── Plusieurs lignes ──────────────────────────────────────────────────
    if len(centres) >= 2:
        ref = centres[0]
        if all(abs(c - ref) <= tol_multi for c in centres):
            return True, ref
        return False, 0

    # ── Une seule ligne → centré sur la page ? ────────────────────────────
    centre_page = page_largeur / 2
    if abs(centres[0] - centre_page) <= tol_page:
        return True, centre_page

    return False, 0



def zone_effacement(page, span, police, taille):
    """
    Calcule la zone exacte à effacer pour un span de texte centré.
    
    Stratégie :
    - Horizontalement : toute la largeur de la zone blanche détectée par pixels
    - Verticalement   : baseline ± marges calculées depuis les métriques réelles
    """
    try:
        import numpy as np
        bbox  = span["bbox"]
        orig  = span["origin"]  # orig[1] = baseline Y

        # ── 1. Largeur : détecter la zone blanche horizontalement ─────────
        # Chercher sur une ligne horizontale au niveau de la baseline
        y_scan = orig[1] - (orig[1] - bbox[1]) * 0.5  # milieu du texte
        zone_h = fitz.Rect(0, y_scan - 2, page.rect.width, y_scan + 2)
        mat = fitz.Matrix(3, 3)
        pix_h = page.get_pixmap(matrix=mat, clip=zone_h)
        img_h = pix_h.tobytes("png")
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(img_h))
        arr = np.array(img)
        masque_h = (arr[:,:,0]>245)&(arr[:,:,1]>245)&(arr[:,:,2]>245)
        cols = np.where(masque_h.any(axis=0))[0]
        if len(cols) > 10:
            x0 = cols[0]  / 3.0
            x1 = cols[-1] / 3.0
        else:
            x0 = bbox[0] - 5
            x1 = bbox[2] + 5

        # ── 2. Hauteur : scanner verticalement sur la largeur détectée ────
        zone_v = fitz.Rect(x0 + 10, max(0, bbox[1]-30),
                           x1 - 10, min(page.rect.height, bbox[3]+30))
        pix_v = page.get_pixmap(matrix=mat, clip=zone_v)
        arr_v = np.array(Image.open(io.BytesIO(pix_v.tobytes("png"))))
        masque_v = (arr_v[:,:,0]>245)&(arr_v[:,:,1]>245)&(arr_v[:,:,2]>245)
        # Garder uniquement les rangées avec >50% de pixels blancs
        seuil = arr_v.shape[1] * 0.5
        dense = np.where(masque_v.sum(axis=1) > seuil)[0]
        if len(dense) > 3:
            y0 = zone_v.y0 + dense[0]  / 3.0
            y1 = zone_v.y0 + dense[-1] / 3.0
        else:
            y0 = orig[1] - (orig[1] - bbox[1]) * 0.85
            y1 = orig[1] + (bbox[3] - orig[1])

        return fitz.Rect(x0, y0, x1, y1)

    except Exception:
        # Fallback : bbox exacte du texte sans marge verticale
        orig = span["origin"]
        _, asc, desc = mesurer_texte(span["text"], police, taille)
        w = largeur_texte(span["text"], police, taille)
        return fitz.Rect(orig[0]-5, orig[1]-asc, orig[0]+w+5, orig[1]+desc)


def mesurer_texte(texte, fontfile, fontsize):
    """
    Mesure la bbox exacte d'un texte rendu avec une police et taille données.
    Retourne (largeur, ascendant, descendant) en pixels.
    """
    try:
        doc_tmp = fitz.open()
        page_tmp = doc_tmp.new_page(width=2000, height=300)
        baseline_y = 150
        page_tmp.insert_text((10, baseline_y), texte, fontfile=fontfile, fontsize=fontsize)
        for b in page_tmp.get_text("dict")["blocks"]:
            if b["type"] == 0:
                for line in b["lines"]:
                    for span in line["spans"]:
                        t = span["text"].strip()
                        if texte.strip() in t or t in texte.strip():
                            bbox = span["bbox"]
                            orig = span["origin"]
                            largeur    = bbox[2] - bbox[0]
                            ascendant  = orig[1] - bbox[1]
                            descendant = bbox[3] - orig[1]
                            return largeur, ascendant, descendant
    except Exception:
        pass
    return len(texte) * fontsize * 0.6, fontsize * 0.75, fontsize * 0.2

def largeur_texte(texte, fontfile, fontsize):
    """Wrapper — retourne uniquement la largeur."""
    return mesurer_texte(texte, fontfile, fontsize)[0]



def personnaliser_pdf_pages(chemin_pdf, prenom_ancien, prenom_nouveau):
    doc = fitz.open(chemin_pdf)
    cache_polices = extraire_polices_pdf(doc)
    total = 0

    for page in doc:
        # Collecter les blocs à réécrire avec leur info de centrage
        blocs_info = []
        for bloc in page.get_text("dict")["blocks"]:
            if bloc["type"] != 0:
                continue
            if not any(prenom_ancien.upper() in span["text"].upper()
                       for line in bloc["lines"] for span in line["spans"]):
                continue
            centre, centre_x = est_bloc_centre(bloc, page.rect.width)
            blocs_info.append({
                "spans": [span for line in bloc["lines"] for span in line["spans"]],
                "centre": centre,
                "centre_x": centre_x
            })

        # ── Étape 1 : effacer ──────────────────────────────────────────────
        for info in blocs_info:
            for span in info["spans"]:
                police_span = police_pour_span(span, cache_polices)
                # Essayer de détecter la zone blanche réelle (cartouche Canva)
                zone = zone_effacement(page, span, police_span, span["size"])
                page.add_redact_annot(zone, fill=(1, 1, 1))
        page.apply_redactions()

        # ── Étape 2 : réécrire avec centrage si nécessaire ─────────────────
        for info in blocs_info:
            for span in info["spans"]:
                texte_nouveau = adapter_casse(prenom_nouveau, span["text"], prenom_ancien)
                police = police_pour_span(span, cache_polices)
                taille = span["size"]

                baseline_y = span["origin"][1]
                w, asc, desc = mesurer_texte(texte_nouveau, police, taille)

                if info["centre"] and prenom_ancien.upper() in span["text"].upper():
                    # ── Centré : recalculer X depuis le centre ────────────
                    x_depart = info["centre_x"] - (w / 2)
                else:
                    x_depart = span["origin"][0]

                page.insert_text(
                    (x_depart, baseline_y),
                    texte_nouveau,
                    fontfile=police,
                    fontsize=taille,
                    color=(0, 0, 0)
                )
                total += 1

    return doc, total

# ── Assemblage PDF final ───────────────────────────────────────────────────
def assembler_pdf(docs, prenom, compression):
    """
    Assemble une liste de fitz.Document en un seul PDF final.
    docs = [doc_couverture, doc_bd] ou [doc_bd] si pas de couverture.
    """
    pdf_final = fitz.open()
    for doc in docs:
        pdf_final.insert_pdf(doc)

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
.loader{display:none;padding:16px 0}
.loader.actif{display:block}
.gen-progress-wrap{height:10px;background:rgba(108,60,225,.1);border-radius:10px;overflow:hidden;margin-bottom:8px}
.gen-progress-bar{height:100%;background:linear-gradient(90deg,var(--violet),var(--vert));width:0%;transition:width .5s ease;border-radius:10px}
.gen-pct{font-family:'Fredoka One',cursive;font-size:1.1rem;color:var(--violet);text-align:center;margin-bottom:4px}
.gen-msg{font-size:.82rem;font-weight:700;color:var(--doux);text-align:center;min-height:1.2em}

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
.progress-wrap{border-radius:10px;overflow:hidden;background:rgba(108,60,225,.08);height:8px;position:relative}
.progress-bar{height:100%;background:linear-gradient(90deg,var(--violet),var(--vert));width:0%;transition:width .3s;border-radius:10px}
.progress-label{font-size:.75rem;font-weight:700;color:var(--doux);text-align:center;margin-top:6px}
.footer{text-align:center;font-size:.72rem;color:var(--doux);opacity:.5;font-weight:600}
.version-badge{display:inline-block;margin-top:6px;font-size:.7rem;font-weight:800;color:var(--doux);opacity:.5;background:rgba(108,60,225,.08);padding:2px 10px;border-radius:20px;letter-spacing:.5px}
</style>
</head>
<body>
<div class="deco">⭐</div><div class="deco">📚</div><div class="deco">🚀</div><div class="deco">💡</div>

<div class="page">
  <div class="header">
    <div class="badge">EnfantProdige</div>
    <h1 class="titre">BD <span>Personnalisée</span></h1>
    <p class="sous-titre">Couverture + BD personnalisée = PDF prêt à envoyer ✨</p>
    <div class="version-badge">v11/05/2026 14:05</div>
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

    <!-- Compression -->
    <label class="label">Compression du PDF final</label>
    <div class="comp-row">
      <button class="comp-btn" onclick="setComp('aucune',this)">📄 Aucune</button>
      <button class="comp-btn actif" onclick="setComp('moyenne',this)">⚖️ Moyenne</button>
      <button class="comp-btn" onclick="setComp('forte',this)">🗜️ Forte</button>
    </div>

    <button class="btn" id="btn-gen" onclick="generer()">🚀 Générer le PDF final</button>

    <div class="loader" id="loader">
      <div class="gen-progress-wrap">
        <div class="gen-progress-bar" id="gen-bar"></div>
      </div>
      <div class="gen-pct" id="gen-pct">0%</div>
      <div class="gen-msg" id="gen-msg">Initialisation…</div>
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

    <!-- Pages BD -->
    <label class="label">Pages BD (PDF Canva)</label>
    <div class="zone-upload" id="zone-up">
      <input type="file" id="input-pdf" accept=".pdf">
      <span class="icone">📄</span>
      <div class="lbl">Glisse le PDF des pages ici</div>
      <div class="sub">Export PDF depuis Canva</div>
      <div class="nom-fichier" id="nom-fich"></div>
    </div>

    <label class="label">Nom de la BD</label>
    <input type="text" class="champ-nom" id="nom-bd" placeholder="Ex : Académie des Génies — Tome 1">

    <label class="label">Prénom placeholder dans les pages</label>
    <input type="text" class="champ-nom" id="prenom-bd" placeholder="Ex : WILLIAM">

    <div class="sep"></div>

    <!-- Couverture PDF -->
    <label class="label">Couverture (PDF Canva — optionnel)</label>
    <div class="zone-upload" id="zone-couv-biblio">
      <input type="file" id="input-couv-biblio" accept=".pdf">
      <span class="icone">🎨</span>
      <div class="lbl">Glisse la couverture ici</div>
      <div class="sub">PDF Canva avec le prénom</div>
      <div class="nom-fichier" id="nom-couv-fich"></div>
    </div>

    <label class="label">Prénom placeholder sur la couverture</label>
    <input type="text" class="champ-nom" id="prenom-couv-bd" placeholder="Ex : WILLIAM (laisser vide = même que pages)">

    <label class="label">Type de couverture</label>
    <div class="comp-row" style="margin-bottom:16px">
      <button class="comp-btn actif" id="btn-couv-sep" onclick="setTypeCouv('separee',this)">📄 Fichier séparé</button>
      <button class="comp-btn" id="btn-couv-int" onclick="setTypeCouv('integree',this)">📋 Intégrée au doc</button>
    </div>
    <div id="hint-type-couv" style="font-size:.72rem;color:var(--doux);margin-top:-12px;margin-bottom:14px">
      La couverture est un PDF Canva séparé des pages
    </div>

    <button class="btn-sm" id="btn-upload-bd" onclick="uploadBD()">➕ Ajouter à la bibliothèque</button>
    <div class="progress-wrap" id="progress-wrap" style="display:none;margin-top:12px">
      <div class="progress-bar" id="progress-bar"></div>
      <div class="progress-label" id="progress-label">Upload en cours…</div>
    </div>
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

document.getElementById('input-couv-biblio').addEventListener('change', function() {
  const f = this.files[0];
  if (f) {
    document.getElementById('zone-couv-biblio').classList.add('ok');
    const el = document.getElementById('nom-couv-fich');
    el.style.display = 'block';
    el.textContent = '✓ ' + f.name;
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

let typeCouv = 'separee';

function setTypeCouv(val, btn) {
  typeCouv = val;
  document.querySelectorAll('#btn-couv-sep, #btn-couv-int').forEach(b => b.classList.remove('actif'));
  btn.classList.add('actif');
  document.getElementById('hint-type-couv').textContent =
    val === 'separee'
      ? 'La couverture est un PDF Canva séparé des pages'
      : 'La couverture est déjà incluse comme première page du document';
  // Masquer la zone upload couverture si intégrée
  document.getElementById('zone-couv-biblio').style.opacity = val === 'integree' ? '.4' : '1';
  document.getElementById('zone-couv-biblio').style.pointerEvents = val === 'integree' ? 'none' : 'auto';
}

function setProgress(pct, label) {
  document.getElementById('progress-bar').style.width = pct + '%';
  document.getElementById('progress-label').textContent = label;
}

async function uploadBD() {
  const f      = inputPdf.files[0];
  const fCouv  = document.getElementById('input-couv-biblio').files[0];
  const nom    = document.getElementById('nom-bd').value.trim();
  const prenom = document.getElementById('prenom-bd').value.trim();
  const prenomCouv = document.getElementById('prenom-couv-bd').value.trim();
  const msgEl  = document.getElementById('msg-upload');
  const btn    = document.getElementById('btn-upload-bd');
  msgEl.className = 'msg';

  if (!f)     { affMsg(msgEl,'Choisis le PDF des pages.','err'); return; }
  if (!nom)   { affMsg(msgEl,'Donne un nom à cette BD.','err'); return; }
  if (!prenom){ affMsg(msgEl,'Indique le prénom placeholder.','err'); return; }

  // UI → chargement
  btn.disabled = true;
  btn.textContent = '⏳ Upload en cours…';
  document.getElementById('progress-wrap').style.display = 'block';
  setProgress(10, 'Préparation…');

  const fd = new FormData();
  fd.append('pdf', f);
  fd.append('nom', nom);
  fd.append('prenom', prenom);
  fd.append('prenom_couv', prenomCouv || prenom);
  fd.append('type_couv', typeCouv);
  if (fCouv && typeCouv === 'separee') fd.append('couverture', fCouv);

  setProgress(30, 'Envoi du fichier…');

  try {
    const res  = await fetch('/ajouter-bd', { method:'POST', body:fd });
    setProgress(80, 'Traitement…');
    const data = await res.json();

    if (data.erreur) {
      affMsg(msgEl, data.erreur, 'err');
      setProgress(0, '');
      document.getElementById('progress-wrap').style.display = 'none';
      return;
    }

    setProgress(100, '✓ BD ajoutée avec succès !');
    setTimeout(() => {
      document.getElementById('progress-wrap').style.display = 'none';
      affMsg(msgEl, '✓ BD ajoutée à la bibliothèque !', 'ok');
    }, 600);

    // Reset
    inputPdf.value = '';
    document.getElementById('input-couv-biblio').value = '';
    zoneUp.classList.remove('ok');
    document.getElementById('zone-couv-biblio').classList.remove('ok');
    nomFich.style.display = 'none';
    document.getElementById('nom-couv-fich').style.display = 'none';
    document.getElementById('nom-bd').value = '';
    document.getElementById('prenom-bd').value = '';
    document.getElementById('prenom-couv-bd').value = '';
    chargerListe(); chargerSelectBD();

  } catch(e) {
    affMsg(msgEl, 'Erreur de connexion.', 'err');
    setProgress(0,'');
    document.getElementById('progress-wrap').style.display = 'none';
  } finally {
    btn.disabled = false;
    btn.textContent = '➕ Ajouter à la bibliothèque';
  }
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
        ${bd.couverture ? '<div class="bd-couv">🎨 Couverture PDF incluse</div>' : ''}
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
  majApercu();
}

function majApercu() {
  const v = document.getElementById('prenom-nouveau').value;
  document.getElementById('ap-apres').textContent = v ? v.toUpperCase() : '…';
}

// ── Étapes loader ────────────────────────────────────────────────────────────
function majProgression(pct, msg) {
  document.getElementById('gen-bar').style.width = pct + '%';
  document.getElementById('gen-pct').textContent = pct + '%';
  document.getElementById('gen-msg').textContent = msg;
}

// ── Génération ────────────────────────────────────────────────────────────────
async function generer() {
  const bdId    = document.getElementById('select-bd').value;
  const nouveau = document.getElementById('prenom-nouveau').value.trim();
  const errEl = document.getElementById('err-perso');
  errEl.className = 'msg err';

  if (!bdId)    { affMsg(errEl,'Sélectionne une BD.','err'); return; }
  if (!nouveau) { affMsg(errEl,"Entre le prénom de l'enfant.",'err'); return; }

  document.getElementById('btn-gen').disabled = true;
  document.getElementById('loader').classList.add('actif');
  document.getElementById('resultat').classList.remove('actif');

  majProgression(0, 'Démarrage…');

  const prenom_cap = nouveau.charAt(0).toUpperCase() + nouveau.slice(1).toLowerCase();

  fetch('/generer', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ bd_id: bdId, prenom_nouveau: nouveau, compression })
  }).then(response => {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    function lire() {
      reader.read().then(({ done, value }) => {
        if (done) {
          document.getElementById('btn-gen').disabled = false;
          return;
        }
        buffer += decoder.decode(value, { stream: true });
        const blocs = buffer.split('\n\n');
        buffer = blocs.pop();

        for (const bloc of blocs) {
          if (!bloc.startsWith('data: ')) continue;
          try {
            const evt = JSON.parse(bloc.slice(6));
            majProgression(evt.pct, evt.msg);

            if (evt.erreur) {
              affMsg(errEl, evt.erreur, 'err');
              document.getElementById('btn-gen').disabled = false;
              document.getElementById('loader').classList.remove('actif');
              return;
            }
            if (evt.succes) {
              document.getElementById('loader').classList.remove('actif');
              document.getElementById('res-titre').textContent = 'PDF de ' + prenom_cap + ' prêt ! 🎉';
              document.getElementById('res-info').textContent =
                evt.pages + ' pages · ' + (evt.avec_couverture ? 'Couverture incluse · ' : '') + 'Compression ' + compression;
              document.getElementById('res-taille').textContent = '📦 ' + evt.taille_mo + ' Mo';
              document.getElementById('btn-dl').href = '/telecharger/' + evt.fichier;
              document.getElementById('btn-dl').download = 'BD_' + prenom_cap + '.pdf';
              document.getElementById('resultat').classList.add('actif');
              document.getElementById('btn-gen').disabled = false;
              return;
            }
          } catch(e) {}
        }
        lire();
      }).catch(e => {
        affMsg(errEl, 'Erreur lecture : ' + e.message, 'err');
        document.getElementById('btn-gen').disabled = false;
        document.getElementById('loader').classList.remove('actif');
      });
    }
    lire();
  }).catch(e => {
    affMsg(errEl, 'Erreur : ' + e.message, 'err');
    document.getElementById('btn-gen').disabled = false;
    document.getElementById('loader').classList.remove('actif');
  });
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
    fichier      = request.files.get("pdf")
    couv_fichier = request.files.get("couverture")
    nom          = request.form.get("nom","").strip()
    prenom_bd    = request.form.get("prenom","").strip().upper()
    prenom_couv  = request.form.get("prenom_couv","").strip().upper()

    if not fichier or not nom or not prenom_bd:
        return jsonify({"erreur":"Données manquantes"}), 400

    bd_id = uuid.uuid4().hex[:10]

    # Sauvegarder les pages BD
    chemin_bd = os.path.join(BIBLIO_FOLDER, f"{bd_id}_bd.pdf")
    fichier.save(chemin_bd)

    # Sauvegarder la couverture si fournie
    chemin_couv = None
    if couv_fichier:
        chemin_couv = os.path.join(BIBLIO_FOLDER, f"{bd_id}_couv.pdf")
        couv_fichier.save(chemin_couv)

    meta = lire_meta()
    meta[bd_id] = {
        "id":          bd_id,
        "nom":         nom,
        "prenom":      prenom_bd,
        "prenom_couv": prenom_couv or prenom_bd,
        "pages":       f"{bd_id}_bd.pdf",
        "couverture":  f"{bd_id}_couv.pdf" if chemin_couv else None,
        "type_couv":   request.form.get("type_couv", "separee")
    }
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

def _stream_generer(bd_id, prenom_nouveau, compression):
    """Générateur SSE — envoie la progression étape par étape."""
    import json as _json

    def evt(pct, msg, data=None):
        payload = {"pct": pct, "msg": msg}
        if data: payload.update(data)
        return "data: " + _json.dumps(payload, ensure_ascii=False) + "\n\n"

    meta = lire_meta()
    if bd_id not in meta:
        yield evt(0, "Erreur", {"erreur": "BD introuvable"})
        return

    bd            = meta[bd_id]
    nom_pages     = bd.get("pages") or bd.get("fichier","")
    chemin_bd     = os.path.join(BIBLIO_FOLDER, nom_pages)
    prenom_ancien = bd["prenom"]
    type_couv     = bd.get("type_couv", "separee")

    if not os.path.exists(chemin_bd):
        yield evt(0, "Erreur", {"erreur": f"Fichier BD introuvable : {nom_pages}"})
        return

    docs_a_assembler = []

    # ── Étape 1 : Couverture ───────────────────────────────────────────────
    if bd.get("couverture") and type_couv == "separee":
        chemin_couv = os.path.join(BIBLIO_FOLDER, bd["couverture"])
        if os.path.exists(chemin_couv):
            yield evt(10, "🎨 Personnalisation de la couverture…")
            try:
                prenom_couv_ancien = bd.get("prenom_couv") or prenom_ancien
                doc_couv, _ = personnaliser_pdf_pages(chemin_couv, prenom_couv_ancien, prenom_nouveau)
                docs_a_assembler.append(doc_couv)
                yield evt(30, "✅ Couverture personnalisée")
            except Exception as e:
                yield evt(0, "Erreur", {"erreur": f"Erreur couverture : {str(e)}"})
                return
    else:
        yield evt(10, "📄 Pas de couverture séparée…")

    # ── Étape 2 : Pages BD ─────────────────────────────────────────────────
    yield evt(35, f"📝 Remplacement de « {prenom_ancien} » par « {prenom_nouveau} »…")
    try:
        doc_bd, nb = personnaliser_pdf_pages(chemin_bd, prenom_ancien, prenom_nouveau)
        docs_a_assembler.append(doc_bd)
    except Exception as e:
        yield evt(0, "Erreur", {"erreur": f"Erreur BD : {str(e)}"})
        return

    if nb == 0:
        yield evt(0, "Erreur", {"erreur": f"'{prenom_ancien}' introuvable dans le PDF"})
        return

    yield evt(65, f"✅ {nb} occurrence(s) remplacée(s) dans la BD")

    # ── Étape 3 : Assemblage ───────────────────────────────────────────────
    yield evt(70, "📎 Assemblage couverture + pages…")
    try:
        chemin_final = assembler_pdf(docs_a_assembler, prenom_nouveau, compression)
    except Exception as e:
        yield evt(0, "Erreur", {"erreur": f"Erreur assemblage : {str(e)}"})
        return

    yield evt(90, f"🗜️ Compression ({compression})…")

    taille_mo = round(os.path.getsize(chemin_final) / (1024*1024), 1)
    nb_pages  = len(fitz.open(chemin_final))

    yield evt(100, f"🎉 PDF prêt — {nb_pages} pages, {taille_mo} Mo", {
        "succes":          True,
        "fichier":         os.path.basename(chemin_final),
        "taille_mo":       taille_mo,
        "pages":           nb_pages,
        "avec_couverture": bool(bd.get("couverture"))
    })


@app.route("/generer", methods=["POST"])
def generer():
    data           = request.json
    bd_id          = data.get("bd_id","")
    prenom_nouveau = data.get("prenom_nouveau","").strip()
    compression    = data.get("compression","moyenne")

    from flask import Response, stream_with_context
    return Response(
        stream_with_context(_stream_generer(bd_id, prenom_nouveau, compression)),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":   "no-cache",
            "X-Accel-Buffering": "no"
        }
    )

@app.route("/telecharger/<nom>")
def telecharger(nom):
    chemin = os.path.join(OUTPUT_FOLDER, nom)
    if not os.path.exists(chemin): return "Fichier introuvable", 404
    return send_file(chemin, as_attachment=True, download_name=nom)

if __name__ == "__main__":
    app.run(debug=True, port=5000)
