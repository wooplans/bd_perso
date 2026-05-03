#!/usr/bin/env python3
"""
personnaliser_pdf.py — Personnalisation automatique d'une BD exportée depuis Canva
Le PDF doit contenir du texte vectoriel (export Canva avec calque texte séparé).

Usage:
    python3 personnaliser_pdf.py --pdf ./william.pdf --ancien WILLIAM --nouveau EMMA
    python3 personnaliser_pdf.py --pdf ./william.pdf --ancien WILLIAM --nouveau AMINATA --sortie ./output
"""

import fitz  # PyMuPDF
import re
import os
import argparse
import sys

# Police BD à utiliser pour le remplacement
POLICE_BD = "/usr/share/fonts/opentype/comic-neue/ComicNeue-Bold.otf"

def trouver_toutes_occurrences(page, prenom: str) -> list:
    """
    Trouve tous les spans contenant le prénom dans une page PDF.
    Retourne la liste des spans avec leurs métadonnées complètes.
    """
    resultats = []
    prenom_upper = prenom.upper()

    blocks = page.get_text("dict")["blocks"]
    for b in blocks:
        if b["type"] != 0:
            continue
        for line in b["lines"]:
            for span in line["spans"]:
                if prenom_upper in span["text"].upper():
                    resultats.append(span)

    return resultats


def adapter_casse(prenom_nouveau: str, texte_original: str, prenom_ancien: str) -> str:
    """
    Remplace le prénom en respectant la casse de l'original.
    Ex: 'WILLIAM, 8 ANS.' → 'EMMA, 8 ANS.'
        'William sauve' → 'Emma sauve'
    """
    def remplacer(match):
        original = match.group(0)
        if original.isupper():
            return prenom_nouveau.upper()
        elif original[0].isupper():
            return prenom_nouveau.capitalize()
        return prenom_nouveau.lower()

    pattern = re.compile(re.escape(prenom_ancien), re.IGNORECASE)
    return pattern.sub(remplacer, texte_original)


def personnaliser_page(page, prenom_ancien: str, prenom_nouveau: str) -> int:
    """
    Remplace toutes les occurrences du prénom dans une page.
    Retourne le nombre de remplacements effectués.
    """
    spans = trouver_toutes_occurrences(page, prenom_ancien)

    if not spans:
        return 0

    # ── Étape 1 : Effacer les anciens textes ──────────────────────────────
    for span in spans:
        bbox = fitz.Rect(span["bbox"])
        # Élargir légèrement pour effacer proprement
        bbox_elargi = fitz.Rect(
            bbox.x0 - 1,
            bbox.y0 - 1,
            bbox.x1 + 1,
            bbox.y1 + 1
        )
        page.add_redact_annot(bbox_elargi, fill=(1, 1, 1))

    page.apply_redactions()

    # ── Étape 2 : Réécrire avec le nouveau prénom ──────────────────────────
    for span in spans:
        texte_nouveau = adapter_casse(prenom_nouveau, span["text"], prenom_ancien)
        x = span["origin"][0]
        y = span["origin"][1]
        taille = span["size"]

        page.insert_text(
            (x, y),
            texte_nouveau,
            fontfile=POLICE_BD,
            fontsize=taille,
            color=(0, 0, 0)
        )

        print(f"  ✓ '{span['text']}' → '{texte_nouveau}' (taille {taille:.0f}pt)")

    return len(spans)


def personnaliser_pdf(chemin_pdf: str, prenom_ancien: str, prenom_nouveau: str, dossier_sortie: str = "./output"):
    """
    Pipeline principal : ouvre le PDF, remplace le prénom, sauvegarde.
    """
    if not os.path.exists(chemin_pdf):
        print(f"❌ Fichier introuvable : {chemin_pdf}")
        sys.exit(1)

    print(f"\n📖 Personnalisation : '{prenom_ancien}' → '{prenom_nouveau}'")
    print(f"📄 Fichier source : {chemin_pdf}\n")

    doc = fitz.open(chemin_pdf)
    total_remplacements = 0

    for i, page in enumerate(doc):
        n = personnaliser_page(page, prenom_ancien, prenom_nouveau)
        total_remplacements += n
        if n == 0:
            print(f"  Page {i+1} : aucune occurrence")

    if total_remplacements == 0:
        print(f"\n⚠️  Aucune occurrence de '{prenom_ancien}' trouvée dans le PDF.")
        print("   Vérifiez que le PDF contient du texte vectoriel (export Canva).")
        sys.exit(1)

    # Sauvegarder
    os.makedirs(dossier_sortie, exist_ok=True)
    nom_base = os.path.splitext(os.path.basename(chemin_pdf))[0]
    # Remplacer le prénom dans le nom de fichier aussi
    nom_sortie = adapter_casse(prenom_nouveau, nom_base, prenom_ancien)
    chemin_sortie = os.path.join(dossier_sortie, f"{nom_sortie}.pdf")

    doc.save(chemin_sortie, garbage=4, deflate=True)

    taille_mo = os.path.getsize(chemin_sortie) / (1024 * 1024)
    print(f"\n✅ {total_remplacements} remplacement(s) effectué(s)")
    print(f"💾 PDF sauvegardé → {chemin_sortie} ({taille_mo:.1f} Mo)\n")

    return chemin_sortie


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Personnalisation BD — remplacement du prénom dans un PDF Canva"
    )
    parser.add_argument("--pdf",     required=True,          help="Chemin du PDF source (ex: william.pdf)")
    parser.add_argument("--ancien",  required=True,          help="Prénom placeholder dans le PDF (ex: WILLIAM)")
    parser.add_argument("--nouveau", required=True,          help="Prénom de l'enfant (ex: EMMA)")
    parser.add_argument("--sortie",  default="./output",     help="Dossier de sortie")
    args = parser.parse_args()

    personnaliser_pdf(args.pdf, args.ancien, args.nouveau, args.sortie)
