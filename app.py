from flask import Flask, request, jsonify, send_file
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

HTML_PAGE = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BD Personnalisée — EnfantProdige</title>
<link href="https://fonts.googleapis.com/css2?family=Fredoka+One&family=Nunito:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
  :root {
    --jaune: #FFD93D;
    --orange: #FF6B35;
    --violet: #6C3CE1;
    --violet-clair: #8B5CF6;
    --vert: #06D6A0;
    --blanc: #FFFFFF;
    --gris-clair: #F4F1FF;
    --texte: #1A1033;
    --texte-doux: #6B5CA5;
    --rouge: #FF4D6D;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    font-family: 'Nunito', sans-serif;
    background: var(--gris-clair);
    min-height: 100vh;
    overflow-x: hidden;
    color: var(--texte);
  }

  /* ── Fond animé ── */
  body::before {
    content: '';
    position: fixed;
    inset: 0;
    background:
      radial-gradient(circle at 15% 20%, rgba(108,60,225,0.12) 0%, transparent 50%),
      radial-gradient(circle at 85% 80%, rgba(255,107,53,0.10) 0%, transparent 50%),
      radial-gradient(circle at 50% 50%, rgba(6,214,160,0.06) 0%, transparent 60%);
    pointer-events: none;
    z-index: 0;
  }

  /* ── Étoiles décoratives ── */
  .deco {
    position: fixed;
    font-size: 2rem;
    opacity: 0.15;
    pointer-events: none;
    z-index: 0;
    animation: flotte 6s ease-in-out infinite;
  }
  .deco:nth-child(1) { top: 8%; left: 5%; animation-delay: 0s; }
  .deco:nth-child(2) { top: 15%; right: 8%; animation-delay: 1s; }
  .deco:nth-child(3) { bottom: 20%; left: 8%; animation-delay: 2s; }
  .deco:nth-child(4) { bottom: 10%; right: 5%; animation-delay: 0.5s; }
  .deco:nth-child(5) { top: 50%; left: 2%; animation-delay: 3s; }

  @keyframes flotte {
    0%, 100% { transform: translateY(0) rotate(0deg); }
    50% { transform: translateY(-12px) rotate(5deg); }
  }

  /* ── Layout principal ── */
  .page {
    position: relative;
    z-index: 1;
    max-width: 540px;
    margin: 0 auto;
    padding: 40px 20px 60px;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 28px;
  }

  /* ── Header ── */
  .header {
    text-align: center;
  }

  .badge-app {
    display: inline-block;
    background: var(--violet);
    color: white;
    font-size: 11px;
    font-weight: 800;
    letter-spacing: 2px;
    text-transform: uppercase;
    padding: 5px 14px;
    border-radius: 20px;
    margin-bottom: 14px;
  }

  .titre {
    font-family: 'Fredoka One', cursive;
    font-size: 2.6rem;
    line-height: 1.1;
    color: var(--texte);
    margin-bottom: 8px;
  }

  .titre span { color: var(--orange); }

  .sous-titre {
    color: var(--texte-doux);
    font-size: 1rem;
    font-weight: 600;
    line-height: 1.5;
  }

  /* ── Carte principale ── */
  .carte {
    width: 100%;
    background: white;
    border-radius: 24px;
    padding: 32px;
    box-shadow:
      0 4px 0 0 rgba(108,60,225,0.15),
      0 8px 40px rgba(108,60,225,0.10);
    border: 2px solid rgba(108,60,225,0.08);
  }

  /* ── Zone de dépôt PDF ── */
  .zone-pdf {
    border: 2.5px dashed rgba(108,60,225,0.3);
    border-radius: 16px;
    padding: 32px 20px;
    text-align: center;
    cursor: pointer;
    transition: all 0.25s;
    background: var(--gris-clair);
    position: relative;
    margin-bottom: 24px;
  }

  .zone-pdf:hover, .zone-pdf.survol {
    border-color: var(--violet);
    background: rgba(108,60,225,0.05);
    transform: scale(1.01);
  }

  .zone-pdf.fichier-ok {
    border-color: var(--vert);
    background: rgba(6,214,160,0.06);
  }

  .zone-pdf input[type="file"] {
    position: absolute;
    inset: 0;
    opacity: 0;
    cursor: pointer;
    width: 100%;
    height: 100%;
  }

  .icone-pdf {
    font-size: 3rem;
    margin-bottom: 10px;
    display: block;
  }

  .zone-pdf .label-principal {
    font-size: 1rem;
    font-weight: 700;
    color: var(--violet);
    margin-bottom: 4px;
  }

  .zone-pdf .label-secondaire {
    font-size: 0.82rem;
    color: var(--texte-doux);
  }

  .nom-fichier {
    display: none;
    margin-top: 10px;
    font-size: 0.85rem;
    font-weight: 700;
    color: var(--vert);
    background: rgba(6,214,160,0.1);
    padding: 6px 12px;
    border-radius: 8px;
  }

  /* ── Champs ── */
  .groupe {
    margin-bottom: 18px;
  }

  .groupe label {
    display: block;
    font-size: 0.78rem;
    font-weight: 800;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    color: var(--texte-doux);
    margin-bottom: 8px;
  }

  .champ {
    width: 100%;
    padding: 14px 18px;
    border-radius: 12px;
    border: 2px solid rgba(108,60,225,0.15);
    font-family: 'Nunito', sans-serif;
    font-size: 1rem;
    font-weight: 700;
    color: var(--texte);
    background: var(--gris-clair);
    transition: border-color 0.2s, box-shadow 0.2s;
    outline: none;
  }

  .champ:focus {
    border-color: var(--violet);
    box-shadow: 0 0 0 4px rgba(108,60,225,0.12);
    background: white;
  }

  .champ::placeholder {
    color: rgba(107, 92, 165, 0.4);
    font-weight: 600;
  }

  /* ── Flèche de transformation ── */
  .fleche-transformation {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 18px;
    padding: 12px 16px;
    background: var(--gris-clair);
    border-radius: 12px;
  }

  .prenom-avant {
    flex: 1;
    text-align: center;
    font-size: 1rem;
    font-weight: 800;
    color: var(--texte-doux);
    opacity: 0.5;
  }

  .fleche-icone {
    font-size: 1.4rem;
    color: var(--orange);
    flex-shrink: 0;
  }

  .prenom-apres {
    flex: 1;
    text-align: center;
    font-size: 1.1rem;
    font-weight: 800;
    color: var(--violet);
    min-height: 1.4em;
  }

  /* ── Bouton ── */
  .btn-generer {
    width: 100%;
    padding: 16px;
    border-radius: 14px;
    border: none;
    background: linear-gradient(135deg, var(--violet) 0%, var(--orange) 100%);
    color: white;
    font-family: 'Fredoka One', cursive;
    font-size: 1.3rem;
    letter-spacing: 0.5px;
    cursor: pointer;
    transition: all 0.2s;
    box-shadow: 0 4px 0 rgba(108,60,225,0.4);
    position: relative;
    overflow: hidden;
  }

  .btn-generer:hover:not(:disabled) {
    transform: translateY(-2px);
    box-shadow: 0 6px 0 rgba(108,60,225,0.4);
  }

  .btn-generer:active:not(:disabled) {
    transform: translateY(2px);
    box-shadow: 0 2px 0 rgba(108,60,225,0.4);
  }

  .btn-generer:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  /* ── Loader ── */
  .loader {
    display: none;
    text-align: center;
    padding: 20px;
  }

  .loader.actif { display: block; }

  .points {
    display: inline-flex;
    gap: 6px;
    margin-bottom: 10px;
  }

  .points span {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--violet);
    animation: rebond 1s ease-in-out infinite;
  }

  .points span:nth-child(2) { animation-delay: 0.15s; background: var(--orange); }
  .points span:nth-child(3) { animation-delay: 0.3s; background: var(--vert); }

  @keyframes rebond {
    0%, 80%, 100% { transform: scale(0.7); opacity: 0.5; }
    40% { transform: scale(1.2); opacity: 1; }
  }

  .loader-texte {
    color: var(--texte-doux);
    font-weight: 700;
    font-size: 0.95rem;
  }

  /* ── Résultat ── */
  .resultat {
    display: none;
    text-align: center;
    padding: 24px;
    border-radius: 16px;
    background: linear-gradient(135deg, rgba(6,214,160,0.08), rgba(108,60,225,0.06));
    border: 2px solid rgba(6,214,160,0.3);
  }

  .resultat.actif { display: block; animation: apparaitre 0.4s ease; }

  @keyframes apparaitre {
    from { opacity: 0; transform: scale(0.95) translateY(10px); }
    to { opacity: 1; transform: scale(1) translateY(0); }
  }

  .resultat-emoji { font-size: 3rem; margin-bottom: 8px; }

  .resultat-titre {
    font-family: 'Fredoka One', cursive;
    font-size: 1.4rem;
    color: var(--texte);
    margin-bottom: 4px;
  }

  .resultat-info {
    font-size: 0.85rem;
    color: var(--texte-doux);
    margin-bottom: 20px;
  }

  .btn-telecharger {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 13px 28px;
    border-radius: 12px;
    background: var(--vert);
    color: white;
    font-family: 'Fredoka One', cursive;
    font-size: 1.1rem;
    text-decoration: none;
    transition: all 0.2s;
    box-shadow: 0 4px 0 rgba(6,214,160,0.4);
  }

  .btn-telecharger:hover {
    transform: translateY(-2px);
    box-shadow: 0 6px 0 rgba(6,214,160,0.4);
  }

  .btn-nouveau {
    display: block;
    margin-top: 12px;
    background: none;
    border: none;
    color: var(--texte-doux);
    font-family: 'Nunito', sans-serif;
    font-size: 0.85rem;
    font-weight: 700;
    cursor: pointer;
    text-decoration: underline;
  }

  /* ── Erreur ── */
  .erreur {
    display: none;
    padding: 14px 18px;
    border-radius: 12px;
    background: rgba(255,77,109,0.08);
    border: 2px solid rgba(255,77,109,0.3);
    color: var(--rouge);
    font-weight: 700;
    font-size: 0.9rem;
    text-align: center;
  }

  .erreur.actif { display: block; }

  /* ── Footer ── */
  .footer {
    text-align: center;
    font-size: 0.78rem;
    color: var(--texte-doux);
    opacity: 0.6;
    font-weight: 600;
  }
</style>
</head>
<body>

<!-- Décorations flottantes -->
<div class="deco">⭐</div>
<div class="deco">📚</div>
<div class="deco">🚀</div>
<div class="deco">💡</div>
<div class="deco">✨</div>

<div class="page">

  <!-- Header -->
  <div class="header">
    <div class="badge-app">EnfantProdige</div>
    <h1 class="titre">BD <span>Personnalisée</span></h1>
    <p class="sous-titre">Ton enfant devient le héros de l'histoire ✨</p>
  </div>

  <!-- Carte formulaire -->
  <div class="carte" id="carte-form">

    <!-- Zone upload PDF -->
    <div class="zone-pdf" id="zone-pdf">
      <input type="file" id="input-pdf" accept=".pdf">
      <span class="icone-pdf">📄</span>
      <div class="label-principal">Choisir la BD à personnaliser</div>
      <div class="label-secondaire">Glisse ton PDF ici ou clique pour choisir</div>
      <div class="nom-fichier" id="nom-fichier"></div>
    </div>

    <!-- Champ prénom dans le PDF -->
    <div class="groupe">
      <label for="prenom-ancien">Prénom du héros dans le PDF</label>
      <input
        type="text"
        id="prenom-ancien"
        class="champ"
        placeholder="Ex : WILLIAM"
        autocomplete="off"
        oninput="mettreAJourApercu()"
      >
    </div>

    <!-- Aperçu transformation -->
    <div class="fleche-transformation">
      <div class="prenom-avant" id="apercu-avant">WILLIAM</div>
      <div class="fleche-icone">→</div>
      <div class="prenom-apres" id="apercu-apres">…</div>
    </div>

    <!-- Champ nouveau prénom -->
    <div class="groupe">
      <label for="prenom-nouveau">Prénom de l'enfant</label>
      <input
        type="text"
        id="prenom-nouveau"
        class="champ"
        placeholder="Ex : AMINATA"
        autocomplete="off"
        oninput="mettreAJourApercu()"
      >
    </div>

    <!-- Bouton générer -->
    <button class="btn-generer" id="btn-generer" onclick="generer()">
      🎨 Générer la BD personnalisée
    </button>

    <!-- Loader -->
    <div class="loader" id="loader">
      <div class="points">
        <span></span><span></span><span></span>
      </div>
      <div class="loader-texte">Personnalisation en cours…</div>
    </div>

    <!-- Erreur -->
    <div class="erreur" id="erreur"></div>

  </div>

  <!-- Carte résultat -->
  <div class="carte resultat" id="resultat">
    <div class="resultat-emoji">🎉</div>
    <div class="resultat-titre" id="resultat-titre">BD prête !</div>
    <div class="resultat-info" id="resultat-info"></div>
    <a href="#" class="btn-telecharger" id="btn-telecharger" download>
      ⬇️ Télécharger le PDF
    </a>
    <button class="btn-nouveau" onclick="nouveau()">
      Personnaliser une autre BD
    </button>
  </div>

  <div class="footer">EnfantProdige · Académie des Génies · Yaoundé</div>

</div>

<script>
  // ── Gestion du fichier ─────────────────────────────────────────────────────
  const inputPdf = document.getElementById('input-pdf');
  const zonePdf  = document.getElementById('zone-pdf');
  const nomFichierEl = document.getElementById('nom-fichier');

  inputPdf.addEventListener('change', () => {
    const f = inputPdf.files[0];
    if (f) {
      zonePdf.classList.add('fichier-ok');
      nomFichierEl.style.display = 'inline-block';
      nomFichierEl.textContent = '✓ ' + f.name;
    }
  });

  // Drag & drop
  zonePdf.addEventListener('dragover', e => { e.preventDefault(); zonePdf.classList.add('survol'); });
  zonePdf.addEventListener('dragleave', () => zonePdf.classList.remove('survol'));
  zonePdf.addEventListener('drop', e => {
    e.preventDefault();
    zonePdf.classList.remove('survol');
    const f = e.dataTransfer.files[0];
    if (f && f.name.endsWith('.pdf')) {
      const dt = new DataTransfer();
      dt.items.add(f);
      inputPdf.files = dt.files;
      zonePdf.classList.add('fichier-ok');
      nomFichierEl.style.display = 'inline-block';
      nomFichierEl.textContent = '✓ ' + f.name;
    }
  });

  // ── Aperçu en temps réel ───────────────────────────────────────────────────
  function mettreAJourApercu() {
    const ancien = document.getElementById('prenom-ancien').value || 'WILLIAM';
    const nouveau = document.getElementById('prenom-nouveau').value;
    document.getElementById('apercu-avant').textContent = ancien.toUpperCase();
    document.getElementById('apercu-apres').textContent = nouveau ? nouveau.toUpperCase() : '…';
  }

  // ── Génération ────────────────────────────────────────────────────────────
  async function generer() {
    const pdf    = inputPdf.files[0];
    const ancien = document.getElementById('prenom-ancien').value.trim();
    const nouveau = document.getElementById('prenom-nouveau').value.trim();
    const erreurEl = document.getElementById('erreur');

    erreurEl.classList.remove('actif');

    if (!pdf)    { afficherErreur('Veuillez choisir un fichier PDF.'); return; }
    if (!ancien) { afficherErreur('Entrez le prénom actuel dans le PDF.'); return; }
    if (!nouveau) { afficherErreur("Entrez le prénom de l'enfant."); return; }

    // UI → loading
    document.getElementById('btn-generer').disabled = true;
    document.getElementById('loader').classList.add('actif');

    const formData = new FormData();
    formData.append('pdf', pdf);
    formData.append('prenom_ancien', ancien);
    formData.append('prenom_nouveau', nouveau);

    try {
      const res = await fetch('/personnaliser', { method: 'POST', body: formData });
      const data = await res.json();

      if (!res.ok || data.erreur) {
        afficherErreur(data.erreur || 'Erreur inattendue.');
        return;
      }

      // Succès
      document.getElementById('loader').classList.remove('actif');
      document.getElementById('carte-form').style.display = 'none';

      document.getElementById('resultat-titre').textContent =
        `BD de ${nouveau.charAt(0).toUpperCase() + nouveau.slice(1).toLowerCase()} prête ! 🎉`;
      document.getElementById('resultat-info').textContent =
        `${data.remplacements} occurrence(s) remplacée(s) avec succès.`;

      const btnDL = document.getElementById('btn-telecharger');
      btnDL.href = `/telecharger/${data.fichier}`;
      btnDL.download = `BD_${nouveau.charAt(0).toUpperCase() + nouveau.slice(1).toLowerCase()}.pdf`;

      document.getElementById('resultat').classList.add('actif');

    } catch(e) {
      afficherErreur('Erreur de connexion. Vérifie que le serveur est lancé.');
    } finally {
      document.getElementById('btn-generer').disabled = false;
      document.getElementById('loader').classList.remove('actif');
    }
  }

  function afficherErreur(msg) {
    const el = document.getElementById('erreur');
    el.textContent = '⚠️ ' + msg;
    el.classList.add('actif');
    document.getElementById('btn-generer').disabled = false;
    document.getElementById('loader').classList.remove('actif');
  }

  function nouveau() {
    document.getElementById('carte-form').style.display = 'block';
    document.getElementById('resultat').classList.remove('actif');
    document.getElementById('prenom-nouveau').value = '';
    document.getElementById('apercu-apres').textContent = '…';
    zonePdf.classList.remove('fichier-ok');
    nomFichierEl.style.display = 'none';
    nomFichierEl.textContent = '';
    inputPdf.value = '';
  }
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return HTML_PAGE

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
