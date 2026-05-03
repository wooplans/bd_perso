from flask import Flask, request, jsonify, send_file, render_template
import fitz
import re
import os
import uuid

app = Flask(__name__)

UPLOAD_FOLDER = "./uploads"
OUTPUT_FOLDER = "./output"

# Police BD — fonctionne en local et sur Railway
import glob
def _trouver_police():
    candidats = [
        "/usr/share/fonts/opentype/comic-neue/ComicNeue-Bold.otf",
        "/usr/share/fonts/truetype/comic-neue/ComicNeue-Bold.otf",
        "/nix/store/**/ComicNeue-Bold.otf",
    ]
    for c in candidats:
        if "*" in c:
            matches = glob.glob(c, recursive=True)
            if matches:
                return matches[0]
        elif os.path.exists(c):
            return c
    return None

POLICE_BD = _trouver_police()

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# ── Logique de personnalisation ───────────────────────────────────────────────

def adapter_casse(prenom_nouveau, texte_original, prenom_ancien):
    def remplacer(match):
        original = match.group(0)
        if original.isupper():
            return prenom_nouveau.upper()
        elif original[0].isupper():
            return prenom_nouveau.capitalize()
        return prenom_nouveau.lower()
    pattern = re.compile(re.escape(prenom_ancien), re.IGNORECASE)
    return pattern.sub(remplacer, texte_original)

def personnaliser_pdf(chemin_pdf, prenom_ancien, prenom_nouveau):
    doc = fitz.open(chemin_pdf)
    total = 0

    for page in doc:
        spans_cibles = []
        blocks = page.get_text("dict")["blocks"]
        for b in blocks:
            if b["type"] != 0:
                continue
            for line in b["lines"]:
                for span in line["spans"]:
                    if prenom_ancien.upper() in span["text"].upper():
                        spans_cibles.append(span)

        for span in spans_cibles:
            bbox = fitz.Rect(span["bbox"])
            page.add_redact_annot(
                fitz.Rect(bbox.x0-1, bbox.y0-1, bbox.x1+1, bbox.y1+1),
                fill=(1,1,1)
            )
        page.apply_redactions()

        for span in spans_cibles:
            texte_nouveau = adapter_casse(prenom_nouveau, span["text"], prenom_ancien)
            page.insert_text(
                span["origin"],
                texte_nouveau,
                fontfile=POLICE_BD,
                fontsize=span["size"],
                color=(0,0,0)
            )
            total += 1

    nom_sortie = f"{prenom_nouveau.capitalize()}_{uuid.uuid4().hex[:6]}.pdf"
    chemin_sortie = os.path.join(OUTPUT_FOLDER, nom_sortie)
    doc.save(chemin_sortie, garbage=4, deflate=True)
    return chemin_sortie, total

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/personnaliser", methods=["POST"])
def personnaliser():
    if "pdf" not in request.files:
        return jsonify({"erreur": "Aucun fichier PDF fourni"}), 400

    fichier = request.files["pdf"]
    prenom_ancien = request.form.get("prenom_ancien", "").strip()
    prenom_nouveau = request.form.get("prenom_nouveau", "").strip()

    if not prenom_ancien or not prenom_nouveau:
        return jsonify({"erreur": "Prénoms manquants"}), 400
    if not fichier.filename.endswith(".pdf"):
        return jsonify({"erreur": "Fichier doit être un PDF"}), 400

    # Sauvegarder le PDF uploadé
    nom_temp = f"{uuid.uuid4().hex}.pdf"
    chemin_temp = os.path.join(UPLOAD_FOLDER, nom_temp)
    fichier.save(chemin_temp)

    try:
        chemin_sortie, nb = personnaliser_pdf(chemin_temp, prenom_ancien, prenom_nouveau)
    except Exception as e:
        return jsonify({"erreur": str(e)}), 500
    finally:
        os.remove(chemin_temp)

    if nb == 0:
        return jsonify({"erreur": f"'{prenom_ancien}' introuvable dans le PDF"}), 404

    nom_fichier = os.path.basename(chemin_sortie)
    return jsonify({
        "succes": True,
        "remplacements": nb,
        "fichier": nom_fichier
    })

@app.route("/telecharger/<nom>")
def telecharger(nom):
    chemin = os.path.join(OUTPUT_FOLDER, nom)
    if not os.path.exists(chemin):
        return "Fichier introuvable", 404
    return send_file(chemin, as_attachment=True, download_name=nom)

if __name__ == "__main__":
    app.run(debug=True, port=5000)
