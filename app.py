from flask import Flask, request, jsonify, send_file
import fitz
import re, os, uuid, json, glob, tempfile
import urllib.request

app = Flask(__name__)

BIBLIO_FOLDER = "./bibliotheque"

def telecharger_drive(url: str, suffixe: str = ".pdf") -> str:
    """
    Télécharge un PDF depuis un lien Google Drive public.
    Supporte les formats :
      - https://drive.google.com/file/d/{ID}/view
      - https://drive.google.com/open?id={ID}
      - https://docs.google.com/...
    Retourne le chemin local du fichier téléchargé.
    """
    import re as _re, urllib.request as _req, urllib.error

    # Extraire l'ID du fichier Drive
    patterns = [
        r"/file/d/([a-zA-Z0-9_-]+)",
        r"[?&]id=([a-zA-Z0-9_-]+)",
        r"/d/([a-zA-Z0-9_-]+)",
    ]
    file_id = None
    for pat in patterns:
        m = _re.search(pat, url)
        if m:
            file_id = m.group(1)
            break

    if not file_id:
        raise ValueError(f"Impossible d'extraire l'ID Google Drive depuis : {url}")

    # URL de téléchargement direct
    download_url = f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t"

    chemin = os.path.join(BIBLIO_FOLDER, f"drive_{uuid.uuid4().hex[:10]}{suffixe}")
    os.makedirs(BIBLIO_FOLDER, exist_ok=True)

    headers = {"User-Agent": "Mozilla/5.0"}
    req = urllib.request.Request(download_url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        with open(chemin, "wb") as f:
            f.write(resp.read())

    # Vérifier que c'est bien un PDF
    with open(chemin, "rb") as f:
        header = f.read(4)
    if header != b"%PDF":
        os.remove(chemin)
        raise ValueError("Le fichier téléchargé n'est pas un PDF valide. Vérifiez que le lien est public.")

    return chemin
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
    Détecte la zone blanche exacte d'un cartouche Canva autour d'un span.

    Méthode :
    - X : scanner juste sous la baseline (zone sans texte) pour trouver
          le segment blanc qui contient le centre du span
    - Y : scanner verticalement sur cette largeur pour trouver les bornes
          hautes/basses de la zone blanche (rangées >50% blanches)
    """
    try:
        import numpy as np, io
        from PIL import Image
        bbox  = span["bbox"]
        orig  = span["origin"]
        mat   = fitz.Matrix(4, 4)
        scale = 4.0
        centre_x = (bbox[0] + bbox[2]) / 2

        # ── 1. Scan horizontal sous la baseline (zone sans lettres) ───────
        y_scan = bbox[3] - 2  # juste sous le bas de la bbox
        zone_h = fitz.Rect(0, y_scan - 0.5, page.rect.width, y_scan + 0.5)
        pix_h  = page.get_pixmap(matrix=mat, clip=zone_h)
        arr_h  = np.array(Image.open(io.BytesIO(pix_h.tobytes("png"))))
        masque_h = (arr_h[:,:,0]>240)&(arr_h[:,:,1]>240)&(arr_h[:,:,2]>240)

        # Trouver le segment blanc qui contient le centre du span
        centre_col = int(centre_x * scale)
        x0_pdf, x1_pdf = bbox[0] - 5, bbox[2] + 5  # fallback
        in_white, seg_start = False, 0
        for col_i in range(masque_h.shape[1]):
            is_w = masque_h[:, col_i].any()
            if is_w and not in_white:
                seg_start = col_i
                in_white  = True
            elif not is_w and in_white:
                if seg_start <= centre_col <= col_i and (col_i - seg_start) > 20:
                    x0_pdf = seg_start / scale
                    x1_pdf = col_i    / scale
                in_white = False
        if in_white and seg_start <= centre_col:
            x0_pdf = seg_start       / scale
            x1_pdf = masque_h.shape[1] / scale

        # ── 2. Scan vertical sur la largeur trouvée ────────────────────────
        marge_v = 30
        zone_v  = fitz.Rect(
            x0_pdf + 10, max(0, bbox[1] - marge_v),
            x1_pdf - 10, min(page.rect.height, bbox[3] + marge_v)
        )
        pix_v  = page.get_pixmap(matrix=mat, clip=zone_v)
        arr_v  = np.array(Image.open(io.BytesIO(pix_v.tobytes("png"))))
        masque_v = (arr_v[:,:,0]>240)&(arr_v[:,:,1]>240)&(arr_v[:,:,2]>240)
        seuil    = arr_v.shape[1] * 0.5
        dense    = np.where(masque_v.sum(axis=1) > seuil)[0]

        if len(dense) > 3:
            y0_pdf = zone_v.y0 + dense[0]  / scale
            y1_pdf = zone_v.y0 + dense[-1] / scale
        else:
            y0_pdf = orig[1] - (orig[1] - bbox[1]) * 0.85
            y1_pdf = bbox[3]

        return fitz.Rect(x0_pdf, y0_pdf, x1_pdf, y1_pdf)

    except Exception:
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

        # ── Collecter les LIGNES contenant le prénom ──────────────────────
        lignes_a_reecrire = []
        for bloc in page.get_text("dict")["blocks"]:
            if bloc["type"] != 0: continue
            for line in bloc["lines"]:
                if not any(prenom_ancien.upper() in span["text"].upper()
                           for span in line["spans"]):
                    continue
                centre, centre_x = est_bloc_centre(bloc, page.rect.width)
                lignes_a_reecrire.append({
                    "spans":    line["spans"],
                    "centre":   centre,
                    "centre_x": centre_x,
                })

        # ── Étape 1 : effacer tous les spans des lignes ciblées ───────────
        for info in lignes_a_reecrire:
            for span in info["spans"]:
                police_span = police_pour_span(span, cache_polices)
                zone = zone_effacement(page, span, police_span, span["size"])
                page.add_redact_annot(zone, fill=(1, 1, 1))
        page.apply_redactions()

        # ── Étape 2 : réécrire chaque ligne en recalculant les positions ──
        for info in lignes_a_reecrire:
            spans     = info["spans"]
            prenom_up = prenom_ancien.upper()

            # Calculer le delta_x : différence de largeur entre ancien et nouveau prénom
            delta_x = 0.0
            for span in spans:
                if prenom_up in span["text"].upper():
                    police  = police_pour_span(span, cache_polices)
                    taille  = span["size"]
                    texte_n = adapter_casse(prenom_nouveau, span["text"], prenom_ancien)
                    w_ancien, _, _ = mesurer_texte(span["text"], police, taille)
                    w_nouveau, _, _ = mesurer_texte(texte_n, police, taille)
                    delta_x = w_nouveau - w_ancien
                    break

            # Réécrire chaque span
            prenom_vu = False
            for span in spans:
                texte_nouveau = adapter_casse(prenom_nouveau, span["text"], prenom_ancien)
                police     = police_pour_span(span, cache_polices)
                taille     = span["size"]
                baseline_y = span["origin"][1]
                contient   = prenom_up in span["text"].upper()

                if info["centre"] and contient:
                    # Centré : recalculer X depuis le centre de la page
                    w_n, _, _ = mesurer_texte(texte_nouveau, police, taille)
                    x_depart = info["centre_x"] - w_n / 2
                elif contient:
                    # Span du prénom : position X originale
                    x_depart = span["origin"][0]
                    prenom_vu = True
                elif prenom_vu and delta_x != 0.0:
                    # Span APRÈS le prénom : décaler du delta
                    x_depart = span["origin"][0] + delta_x
                else:
                    # Span AVANT le prénom : position inchangée
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
def compresser_images_pdf(doc, qualite_jpeg: int, max_dim: int = 0):
    """
    Recompresse toutes les images PNG/JPEG d'un PDF en JPEG optimisé.
    - qualite_jpeg : qualité JPEG (1-100)
    - max_dim      : redimensionner si une dimension dépasse cette valeur (0 = pas de resize)
    """
    from PIL import Image as PILImage
    import io as _io

    for page_num in range(len(doc)):
        for img_info in doc.get_page_images(page_num, full=True):
            xref = img_info[0]
            try:
                base = doc.extract_image(xref)
                data = base["image"]
                if len(data) < 3000:
                    continue  # ignorer les petites images (icônes, etc.)

                img = PILImage.open(_io.BytesIO(data)).convert("RGB")
                w, h = img.size

                # Redimensionner si demandé
                if max_dim > 0 and max(w, h) > max_dim:
                    ratio = max_dim / max(w, h)
                    img = img.resize((int(w * ratio), int(h * ratio)), PILImage.LANCZOS)

                buf = _io.BytesIO()
                img.save(buf, format="JPEG", quality=qualite_jpeg, optimize=True)
                new_data = buf.getvalue()

                # Ne remplacer que si ça réduit vraiment
                if len(new_data) < len(data):
                    doc.update_stream(xref, new_data)

            except Exception:
                pass

    return doc


def aplatir_pdf(doc, dpi: int, qualite_jpeg: int) -> fitz.Document:
    """
    Aplatit un PDF : rasterise chaque page en JPEG puis reconstruit un PDF.
    Élimine tous les calques et redondances — réduction maximale.
    """
    from PIL import Image as PILImage
    import io as _io

    mat      = fitz.Matrix(dpi / 72, dpi / 72)
    doc_flat = fitz.open()

    for page in doc:
        pix  = page.get_pixmap(matrix=mat, alpha=False)
        img  = PILImage.frombytes("RGB", [pix.width, pix.height], pix.samples)
        buf  = _io.BytesIO()
        img.save(buf, format="JPEG", quality=qualite_jpeg, optimize=True)
        page_new = doc_flat.new_page(width=page.rect.width, height=page.rect.height)
        page_new.insert_image(page_new.rect, stream=buf.getvalue())

    return doc_flat


def assembler_pdf(docs, prenom, compression):
    """
    Assemble et compresse le PDF final.

    Niveaux :
    - aucune  : assemblage simple, aucun retraitement
    - moyenne : recompression JPEG q=70 + resize max 1200px → ~-75%
    - forte   : aplatissement complet 150dpi JPEG q=80 → ~-85%
    """
    pdf_final = fitz.open()
    for doc in docs:
        pdf_final.insert_pdf(doc)

    nom    = f"BD_{prenom.capitalize()}_{uuid.uuid4().hex[:6]}.pdf"
    chemin = os.path.join(OUTPUT_FOLDER, nom)

    if compression == "forte":
        # Aplatissement complet : rasterise tout → JPEG
        pdf_final = aplatir_pdf(pdf_final, dpi=150, qualite_jpeg=80)
        pdf_final.save(chemin, garbage=4, deflate=True)
    elif compression == "moyenne":
        # Recompression intelligente : garde le texte vectoriel
        pdf_final = compresser_images_pdf(pdf_final, qualite_jpeg=70, max_dim=1200)
        pdf_final.save(chemin, garbage=4, deflate=True, clean=True)
    else:
        # Même sans compression d'images, optimiser le flux PDF
        pdf_final.save(chemin, garbage=4, deflate=True, clean=True)

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
.resultat{display:none;text-align:center;padding:24px;border-radius:20px}
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
    <p class="sous-titre">BD personnalisée = PDF prêt à envoyer ✨</p>
    <div class="version-badge">v15/05/2026 08:40</div>
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
  </div>

  <!-- Carte résultat (indépendante) -->
  <div class="carte resultat" id="resultat">
    <div class="res-emoji">🎉</div>
    <div class="res-titre" id="res-titre">PDF prêt !</div>
    <div class="res-info" id="res-info"></div>
    <div class="res-taille" id="res-taille"></div>
    <br>
    <a href="#" class="btn-dl" id="btn-dl" download>⬇️ Télécharger le PDF</a>
    <button class="btn-nouveau" onclick="nouveau()">Personnaliser une autre BD</button>
  </div>

  <!-- ═══ ONGLET BIBLIOTHÈQUE ═══ -->
  <div class="carte" style="display:none" id="carte-biblio">

    <!-- Toggle source : Upload ou Google Drive -->
    <div class="comp-row" style="margin-bottom:16px">
      <button class="comp-btn actif" id="btn-src-upload" onclick="setSource('upload',this)">📁 Upload fichier</button>
      <button class="comp-btn" id="btn-src-drive" onclick="setSource('drive',this)">🔗 Lien Google Drive</button>
    </div>

    <!-- === PAGES BD === -->
    <label class="label">Pages BD</label>

    <!-- Upload -->
    <div id="src-upload-bd">
      <div class="zone-upload" id="zone-up">
        <input type="file" id="input-pdf" accept=".pdf">
        <span class="icone">📄</span>
        <div class="lbl">Glisse le PDF des pages ici</div>
        <div class="sub">Export PDF depuis Canva</div>
        <div class="nom-fichier" id="nom-fich"></div>
      </div>
    </div>

    <!-- Drive -->
    <div id="src-drive-bd" style="display:none">
      <input type="text" class="champ-nom" id="lien-drive-bd"
        placeholder="https://drive.google.com/file/d/…/view">
      <div style="font-size:.72rem;color:var(--doux);margin-bottom:12px">
        💡 Partage le fichier Drive en "Tout le monde peut voir"
      </div>
    </div>

    <label class="label">Nom de la BD</label>
    <input type="text" class="champ-nom" id="nom-bd" placeholder="Ex : Académie des Génies — Tome 1">

    <label class="label">Prénom placeholder dans les pages</label>
    <input type="text" class="champ-nom" id="prenom-bd" placeholder="Ex : WILLIAM">

    <button class="btn-sm" id="btn-upload-bd" onclick="uploadBD()">➕ Ajouter à la bibliothèque</button>
    <div class="progress-wrap" id="progress-wrap" style="display:none;margin-top:12px">
      <div class="progress-bar" id="progress-bar"></div>
      <div class="progress-label" id="progress-label">Chargement en cours…</div>
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

let srcMode   = 'upload';  // 'upload' ou 'drive'

function setSource(val, btn) {
  srcMode = val;
  document.querySelectorAll('#btn-src-upload, #btn-src-drive').forEach(b => b.classList.remove('actif'));
  btn.classList.add('actif');
  document.getElementById('src-upload-bd').style.display = val === 'upload' ? 'block' : 'none';
  document.getElementById('src-drive-bd').style.display  = val === 'drive'  ? 'block' : 'none';
}

function setProgress(pct, label) {
  document.getElementById('progress-bar').style.width = pct + '%';
  document.getElementById('progress-label').textContent = label;
}

async function uploadBD() {
  const nom        = document.getElementById('nom-bd').value.trim();
  const prenom     = document.getElementById('prenom-bd').value.trim();

  const msgEl      = document.getElementById('msg-upload');
  const btn        = document.getElementById('btn-upload-bd');
  msgEl.className  = 'msg';

  // Récupérer les sources selon le mode
  const f        = inputPdf.files[0];
  const lienBd   = document.getElementById('lien-drive-bd').value.trim();

  if (srcMode === 'upload' && !f)    { affMsg(msgEl,'Choisis le PDF des pages.','err'); return; }
  if (srcMode === 'drive'  && !lienBd){ affMsg(msgEl,'Colle un lien Google Drive pour les pages.','err'); return; }
  if (!nom)   { affMsg(msgEl,'Donne un nom à cette BD.','err'); return; }
  if (!prenom){ affMsg(msgEl,'Indique le prénom placeholder.','err'); return; }

  btn.disabled = true;
  btn.textContent = '⏳ Chargement en cours…';
  document.getElementById('progress-wrap').style.display = 'block';
  setProgress(10, srcMode === 'drive' ? 'Téléchargement depuis Drive…' : 'Préparation…');

  const fd = new FormData();
  fd.append('nom', nom);
  fd.append('prenom', prenom);
  if (srcMode === 'upload') {
    fd.append('pdf', f);
  } else {
    fd.append('lien_drive_bd', lienBd);
  }

  setProgress(30, 'Envoi en cours…');

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
    if (inputPdf) inputPdf.value = '';
    document.getElementById('lien-drive-bd').value = '';
    if (zoneUp) zoneUp.classList.remove('ok');
    if (nomFich) nomFich.style.display = 'none';
    document.getElementById('nom-bd').value = '';
    document.getElementById('prenom-bd').value = '';
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
        ${bd.source === 'upload' ? '' : '<div class="bd-couv">🔗 Source : Google Drive</div>'}
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
                evt.pages + ' pages · Compression ' + compression;
              document.getElementById('res-taille').textContent = '📦 ' + evt.taille_mo + ' Mo';
              document.getElementById('btn-dl').href = '/telecharger/' + evt.fichier;
              document.getElementById('btn-dl').download = 'BD_' + prenom_cap + '.pdf';
              // Masquer le formulaire, afficher le résultat
              document.getElementById('carte-perso').style.display = 'none';
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
  document.getElementById('carte-perso').style.display = 'block';
  document.getElementById('prenom-nouveau').value = '';
  document.getElementById('ap-apres').textContent = '…';
  majProgression(0, '');
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
    lien_bd   = request.form.get("lien_drive_bd","").strip()
    nom       = request.form.get("nom","").strip()
    prenom_bd = request.form.get("prenom","").strip().upper()

    if not nom or not prenom_bd:
        return jsonify({"erreur":"Nom et prénom obligatoires"}), 400
    if not fichier and not lien_bd:
        return jsonify({"erreur":"Fournis un fichier PDF ou un lien Google Drive"}), 400

    bd_id     = uuid.uuid4().hex[:10]
    chemin_bd = os.path.join(BIBLIO_FOLDER, f"{bd_id}_bd.pdf")

    if fichier:
        fichier.save(chemin_bd)
    elif lien_bd:
        try:
            chemin_tmp = telecharger_drive(lien_bd)
            os.rename(chemin_tmp, chemin_bd)
        except Exception as e:
            return jsonify({"erreur": f"Erreur téléchargement : {str(e)}"}), 400

    meta = lire_meta()
    meta[bd_id] = {
        "id":       bd_id,
        "nom":      nom,
        "prenom":   prenom_bd,
        "pages":    f"{bd_id}_bd.pdf",
        "source":   lien_bd or "upload"
    }
    ecrire_meta(meta)
    return jsonify({"succes":True,"id":bd_id})

@app.route("/liste-bds")
def liste_bds():
    meta = lire_meta()
    bds  = []
    for bd in meta.values():
        bds.append({
            "id":          bd["id"],
            "nom":         bd["nom"],
            "prenom":      bd["prenom"],
            "prenom_couv": bd.get("prenom_couv",""),
            "couverture":  bool(bd.get("couverture")),
            "type_couv":   bd.get("type_couv","separee"),
            "source_bd":   bd.get("source_bd","upload"),
        })
    return jsonify({"bds": bds})

@app.route("/supprimer-bd/<bd_id>", methods=["DELETE"])
def supprimer_bd(bd_id):
    meta = lire_meta()
    if bd_id not in meta: return jsonify({"erreur":"BD introuvable"}), 404
    bd = meta[bd_id]
    # Supprimer le fichier PDF
    nom_fichier = bd.get("pages") or bd.get("fichier", "")
    if nom_fichier:
        chemin = os.path.join(BIBLIO_FOLDER, nom_fichier)
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
    if not os.path.exists(chemin_bd):
        yield evt(0, "Erreur", {"erreur": f"Fichier BD introuvable : {nom_pages}"})
        return

    docs_a_assembler = []
    yield evt(10, "📄 Fichier BD chargé…")

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
    yield evt(70, "📎 Assemblage du PDF…")
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


# ══════════════════════════════════════════════════════════════════════════════
# WEBHOOK CHARIOW — Réception paiement + génération PDF automatique
# ══════════════════════════════════════════════════════════════════════════════

import threading
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders

# Variables d'environnement pour l'envoi email
SMTP_HOST     = os.environ.get("SMTP_HOST", "")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER     = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASS", "")
EMAIL_FROM    = os.environ.get("EMAIL_FROM", "")
APP_URL       = os.environ.get("APP_URL", "https://bd-personnalisee.onrender.com")
CHARIOW_SECRET = os.environ.get("CHARIOW_WEBHOOK_SECRET", "")

# ── Clé de déduplication (évite les doublons en cas de retry Chariow) ────────
_processed_sales = set()


def envoyer_email_pdf(destinataire: str, prenom: str, chemin_pdf: str, nom_bd: str):
    """Envoie le PDF personnalisé par email au client."""
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_FROM]):
        print(f"⚠️ SMTP non configuré — email non envoyé à {destinataire}")
        return False
    try:
        msg = MIMEMultipart()
        msg["From"]    = EMAIL_FROM
        msg["To"]      = destinataire
        msg["Subject"] = f"📚 Ta BD personnalisée est prête, {prenom} !"

        corps = f"""Bonjour,

Ta BD personnalisée "{nom_bd}" avec le prénom {prenom} est prête !

Tu peux télécharger ton PDF en pièce jointe de cet email.

Bonne lecture ! 🎉

— EnfantProdige
"""
        msg.attach(MIMEText(corps, "plain", "utf-8"))

        # Attacher le PDF
        with open(chemin_pdf, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        nom_fichier = f"BD_{prenom}.pdf"
        part.add_header("Content-Disposition", f"attachment; filename={nom_fichier}")
        msg.attach(part)

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, destinataire, msg.as_string())

        print(f"✅ Email envoyé à {destinataire}")
        return True
    except Exception as e:
        print(f"❌ Erreur email : {e}")
        return False


def traiter_commande(sale_id: str, prenom: str, email_client: str,
                     bd_id: str, compression: str = "moyenne"):
    """
    Traitement asynchrone : personnalise la BD et envoie l'email.
    Appelé dans un thread séparé pour répondre 200 à Chariow immédiatement.
    """
    print(f"🔄 Traitement commande {sale_id} — prénom: {prenom} — BD: {bd_id}")

    meta = lire_meta()
    if bd_id not in meta:
        print(f"❌ BD introuvable : {bd_id}")
        return

    bd = meta[bd_id]
    nom_bd        = bd["nom"]
    nom_pages     = bd.get("pages") or bd.get("fichier", "")
    chemin_bd     = os.path.join(BIBLIO_FOLDER, nom_pages)
    prenom_ancien = bd["prenom"]
    if not os.path.exists(chemin_bd):
        print(f"❌ Fichier BD introuvable : {chemin_bd}")
        return

    docs = []
    # Pages BD
    try:
        doc_bd, nb = personnaliser_pdf_pages(chemin_bd, prenom_ancien, prenom)
        docs.append(doc_bd)
        print(f"✅ {nb} remplacement(s) effectué(s)")
    except Exception as e:
        print(f"❌ Erreur BD : {e}")
        return

    if not docs:
        print("❌ Aucun document à assembler")
        return

    # Assemblage
    try:
        chemin_final = assembler_pdf(docs, prenom, compression)
        print(f"✅ PDF généré : {chemin_final}")
    except Exception as e:
        print(f"❌ Erreur assemblage : {e}")
        return

    # Envoi email
    envoyer_email_pdf(email_client, prenom, chemin_final, nom_bd)


@app.route("/api/webhook/chariow", methods=["POST"])
def webhook_chariow():
    """
    Reçoit le webhook Chariow (successful.sale) et déclenche la génération PDF.

    Champs attendus dans custom_fields ou custom_metadata :
      - prenom_enfant : prénom à personnaliser (obligatoire)
      - bd_id         : identifiant de la BD dans la bibliothèque (obligatoire)
      - compression   : aucune | moyenne | forte (optionnel, défaut: moyenne)
    """
    # ── 1. Répondre 200 immédiatement (Chariow attend < 30s) ─────────────────
    data = request.get_json(silent=True) or {}

    # ── 2. Vérifier que c'est bien une vente réussie ─────────────────────────
    event = data.get("event", "")
    if event != "successful.sale":
        return jsonify({"status": "ignored", "event": event}), 200

    sale    = data.get("sale", {})
    sale_id = sale.get("id", "")
    customer = data.get("customer", {})
    email_client = customer.get("email", "")

    # ── 3. Déduplication (retry Chariow) ──────────────────────────────────────
    if sale_id in _processed_sales:
        return jsonify({"status": "duplicate", "sale_id": sale_id}), 200
    _processed_sales.add(sale_id)

    # ── 4. Récupérer le prénom et le bd_id ────────────────────────────────────
    # Chercher dans custom_fields ET custom_metadata
    custom_fields   = sale.get("custom_fields")   or {}
    custom_metadata = sale.get("custom_metadata") or {}
    all_custom = {**custom_metadata, **custom_fields}

    prenom      = all_custom.get("prenom_enfant", "").strip()
    bd_id       = all_custom.get("bd_id", "").strip()
    compression = all_custom.get("compression", "moyenne").strip()

    if not prenom:
        return jsonify({"status": "erreur", "message": "prenom_enfant manquant"}), 400
    if not bd_id:
        return jsonify({"status": "erreur", "message": "bd_id manquant"}), 400

    # ── 5. Lancer le traitement en arrière-plan ───────────────────────────────
    thread = threading.Thread(
        target=traiter_commande,
        args=(sale_id, prenom, email_client, bd_id, compression),
        daemon=True
    )
    thread.start()

    return jsonify({
        "status": "accepted",
        "sale_id": sale_id,
        "prenom": prenom,
        "bd_id": bd_id
    }), 200


@app.route("/api/generer-bd", methods=["POST"])
def api_generer_bd():
    """
    Route API directe pour générer une BD personnalisée.
    Utile pour les intégrations custom (site web, app mobile, etc.)

    Body JSON attendu :
    {
        "bd_id": "identifiant_bd",
        "prenom": "AMINATA",
        "email": "parent@email.com",    (optionnel — pour envoi email)
        "compression": "moyenne"         (optionnel)
    }

    Retourne : { succes, fichier, taille_mo, pages, lien_telechargement }
    """
    data = request.get_json(silent=True) or {}

    bd_id       = data.get("bd_id", "").strip()
    prenom      = data.get("prenom", "").strip()
    email       = data.get("email", "").strip()
    compression = data.get("compression", "moyenne").strip()

    if not bd_id:
        return jsonify({"erreur": "bd_id manquant"}), 400
    if not prenom:
        return jsonify({"erreur": "prenom manquant"}), 400

    meta = lire_meta()
    if bd_id not in meta:
        return jsonify({"erreur": f"BD introuvable : {bd_id}"}), 404

    bd            = meta[bd_id]
    nom_pages     = bd.get("pages") or bd.get("fichier", "")
    chemin_bd     = os.path.join(BIBLIO_FOLDER, nom_pages)
    prenom_ancien = bd["prenom"]
    if not os.path.exists(chemin_bd):
        return jsonify({"erreur": "Fichier BD introuvable sur le serveur"}), 404

    docs = []

    # Pages BD
    try:
        doc_bd, nb = personnaliser_pdf_pages(chemin_bd, prenom_ancien, prenom)
        docs.append(doc_bd)
    except Exception as e:
        return jsonify({"erreur": f"Erreur BD : {str(e)}"}), 500

    if nb == 0:
        return jsonify({"erreur": f"'{prenom_ancien}' introuvable dans le PDF"}), 404

    # Assemblage
    try:
        chemin_final = assembler_pdf(docs, prenom, compression)
    except Exception as e:
        return jsonify({"erreur": f"Erreur assemblage : {str(e)}"}), 500

    nom_fichier = os.path.basename(chemin_final)
    taille_mo   = round(os.path.getsize(chemin_final) / (1024*1024), 1)
    nb_pages    = len(fitz.open(chemin_final))
    lien        = f"{APP_URL}/telecharger/{nom_fichier}"

    # Envoi email si fourni
    if email:
        threading.Thread(
            target=envoyer_email_pdf,
            args=(email, prenom, chemin_final, bd["nom"]),
            daemon=True
        ).start()

    return jsonify({
        "succes":               True,
        "fichier":              nom_fichier,
        "taille_mo":            taille_mo,
        "pages":                nb_pages,
        "lien_telechargement":  lien,
        "avec_couverture":      bool(bd.get("couverture"))
    }), 200


@app.route("/api/bds", methods=["GET"])
def api_liste_bds():
    """
    Liste toutes les BDs disponibles dans la bibliothèque.
    Utile pour construire un sélecteur côté site web.
    """
    meta = lire_meta()
    bds  = [
        {
            "id":          bd["id"],
            "nom":         bd["nom"],
            "prenom":      bd["prenom"],
            "couverture":  bool(bd.get("couverture")),
            "type_couv":   bd.get("type_couv", "separee")
        }
        for bd in meta.values()
    ]
    return jsonify({"bds": bds}), 200

if __name__ == "__main__":
    app.run(debug=True, port=5000)
