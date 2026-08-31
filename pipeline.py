#!/usr/bin/env python3
"""
Chaîne de veille podcasts :
  flux RSS -> téléchargement MP3 -> transcription (faster-whisper)
           -> verbatim (verbatims/) -> synthèse via GitHub Models (syntheses/)

Conçu pour tourner dans GitHub Actions, sans machine personnelle.
État persistant : traites.json (identifiants des épisodes déjà traités).
"""

import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests

# ---------------------------------------------------------------- paramètres

RACINE = Path(__file__).parent
FICHIER_FLUX = RACINE / "feeds.txt"
FICHIER_ETAT = RACINE / "traites.json"
DOSSIER_VERBATIMS = RACINE / "verbatims"
DOSSIER_SYNTHESES = RACINE / "syntheses"

# Nombre maximal d'épisodes traités par exécution (pour rester dans les
# limites de durée de GitHub Actions ; le reste sera pris au passage suivant).
MAX_EPISODES_PAR_RUN = int(os.environ.get("MAX_EPISODES", "6"))

# Modèle de transcription : "small" = bon compromis vitesse/qualité en français.
# Passer à "medium" pour plus de fidélité (plus lent), "base" pour plus de débit.
MODELE_WHISPER = os.environ.get("MODELE_WHISPER", "small")

# À la première exécution, on n'aspire pas tout l'historique :
# seuls les N épisodes les plus récents de chaque flux sont traités.
EPISODES_INITIAUX_PAR_FLUX = 2

# Synthèse via GitHub Models (gratuit avec le jeton du dépôt).
MODELE_SYNTHESE = os.environ.get("MODELE_SYNTHESE", "openai/gpt-4o-mini")
URL_MODELS = "https://models.github.ai/inference/chat/completions"

PROMPT_SYNTHESE = """Tu reçois le verbatim brut (transcription automatique, \
donc avec des coquilles) d'un épisode de podcast.

Rédige en français une synthèse complète mais resserrée, destinée à un \
lecteur exigeant qui veut UNIQUEMENT la valeur ajoutée. Consignes :
1. Concentre-toi sur ce qui est NOVATEUR et SPÉCIFIQUE : thèses originales, \
faits précis, chiffres, exemples concrets, désaccords entre intervenants, \
annonces, raisonnements inattendus.
2. Supprime impitoyablement le remplissage : banalités, autopromotion, \
politesse, généralités connues de tous, digressions sans contenu.
3. Structure : d'abord 3 à 5 phrases donnant l'essentiel de l'épisode ; \
puis les points saillants développés (avec les chiffres et noms propres \
cités) ; enfin, s'il y a lieu, une rubrique « Réserves » signalant les \
affirmations douteuses ou non étayées.
4. Si l'épisode est creux, dis-le franchement en deux phrases plutôt que \
de gonfler artificiellement la synthèse.
Longueur cible : 300 à 700 mots selon la densité réelle de l'épisode."""

# ---------------------------------------------------------------- utilitaires


def journal(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def slug(texte: str, longueur: int = 70) -> str:
    texte = unicodedata.normalize("NFKD", texte).encode("ascii", "ignore").decode()
    texte = re.sub(r"[^A-Za-z0-9]+", "-", texte).strip("-")
    return texte[:longueur] or "sans-titre"


def charger_etat() -> dict:
    if FICHIER_ETAT.exists():
        return json.loads(FICHIER_ETAT.read_text(encoding="utf-8"))
    return {}


def sauver_etat(etat: dict) -> None:
    FICHIER_ETAT.write_text(
        json.dumps(etat, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def lire_flux() -> list[tuple[str, str]]:
    flux = []
    vus = set()

    def ajouter(nom: str, url: str) -> None:
        if url not in vus:
            vus.add(url)
            flux.append((slug(nom, 40), url))

    # 1. feeds.txt (ajouts manuels)
    if FICHIER_FLUX.exists():
        for ligne in FICHIER_FLUX.read_text(encoding="utf-8").splitlines():
            ligne = ligne.strip()
            if not ligne or ligne.startswith("#"):
                continue
            if "|" not in ligne:
                journal(f"Ligne ignorée (format attendu 'Nom | URL') : {ligne}")
                continue
            nom, url = (p.strip() for p in ligne.split("|", 1))
            ajouter(nom, url)

    # 2. Tout fichier .opml ou .xml exporté d'Inoreader, déposé à la racine
    import xml.etree.ElementTree as ET

    for chemin in list(RACINE.glob("*.opml")) + list(RACINE.glob("*.xml")):
        try:
            arbre = ET.parse(chemin)
        except ET.ParseError:
            continue
        n = 0
        for outline in arbre.iter("outline"):
            url = outline.get("xmlUrl")
            if url:
                ajouter(outline.get("title") or outline.get("text") or url, url)
                n += 1
        if n:
            journal(f"OPML « {chemin.name} » : {n} flux importés.")
    return flux


# ---------------------------------------------------------------- transcription

_modele_whisper = None


def transcrire(chemin_audio: Path) -> str:
    global _modele_whisper
    from faster_whisper import WhisperModel

    if _modele_whisper is None:
        journal(f"Chargement du modèle Whisper « {MODELE_WHISPER} »…")
        _modele_whisper = WhisperModel(
            MODELE_WHISPER, device="cpu", compute_type="int8"
        )
    segments, info = _modele_whisper.transcribe(
        str(chemin_audio), vad_filter=True, beam_size=1
    )
    morceaux = []
    for seg in segments:
        morceaux.append(seg.text.strip())
    journal(f"Transcrit ({info.language}, {info.duration/60:.0f} min d'audio).")
    return "\n".join(morceaux)


# ---------------------------------------------------------------- synthèse


def decouper(texte: str, taille: int = 60000) -> list[str]:
    return [texte[i : i + taille] for i in range(0, len(texte), taille)]


def appel_modele(messages: list[dict]) -> str:
    jeton = os.environ.get("GITHUB_TOKEN")
    if not jeton:
        raise RuntimeError("GITHUB_TOKEN absent : synthèse impossible.")
    for tentative in range(4):
        r = requests.post(
            URL_MODELS,
            headers={
                "Authorization": f"Bearer {jeton}",
                "Content-Type": "application/json",
            },
            json={
                "model": MODELE_SYNTHESE,
                "messages": messages,
                "temperature": 0.3,
                "max_tokens": 1800,
            },
            timeout=180,
        )
        if r.status_code == 429:  # limite de débit : on patiente
            attente = 30 * (tentative + 1)
            journal(f"Limite de débit du service de synthèse ; pause {attente} s.")
            time.sleep(attente)
            continue
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    raise RuntimeError("Service de synthèse indisponible (limites de débit).")


def synthetiser(verbatim: str, titre: str, emission: str) -> str:
    tranches = decouper(verbatim)
    if len(tranches) == 1:
        contenu = tranches[0]
    else:
        # Épisode très long : condensation par tranches, puis synthèse finale.
        notes = []
        for i, t in enumerate(tranches, 1):
            journal(f"  condensation de la tranche {i}/{len(tranches)}…")
            notes.append(
                appel_modele(
                    [
                        {
                            "role": "user",
                            "content": "Condense fidèlement ce fragment de "
                            "transcription en gardant tous les faits, chiffres "
                            "et arguments précis, sans remplissage :\n\n" + t,
                        }
                    ]
                )
            )
        contenu = "\n\n".join(notes)
    return appel_modele(
        [
            {"role": "system", "content": PROMPT_SYNTHESE},
            {
                "role": "user",
                "content": f"Émission : {emission}\nÉpisode : {titre}\n\n"
                f"Verbatim :\n{contenu}",
            },
        ]
    )


# ---------------------------------------------------------------- boucle principale


def url_audio(entree) -> str | None:
    for enc in entree.get("enclosures", []):
        if "audio" in enc.get("type", "") or enc.get("href", "").endswith(
            (".mp3", ".m4a", ".ogg")
        ):
            return enc.get("href")
    for lien in entree.get("links", []):
        if lien.get("rel") == "enclosure":
            return lien.get("href")
    return None


def principal() -> None:
    etat = charger_etat()
    traites_total = 0

    for nom, url in lire_flux():
        if traites_total >= MAX_EPISODES_PAR_RUN:
            break
        journal(f"Flux « {nom} » : {url}")
        try:
            flux = feedparser.parse(url)
        except Exception as e:  # noqa: BLE001
            journal(f"  échec de lecture du flux : {e}")
            continue
        if not flux.entries:
            journal("  aucun épisode trouvé.")
            continue

        deja = set(etat.get(nom, []))
        premiere_fois = nom not in etat
        entrees = flux.entries[: EPISODES_INITIAUX_PAR_FLUX] if premiere_fois else flux.entries

        for entree in entrees:
            if traites_total >= MAX_EPISODES_PAR_RUN:
                break
            ident = entree.get("id") or entree.get("link") or entree.get("title", "")
            if not ident or ident in deja:
                continue
            titre = entree.get("title", "sans titre")
            audio = url_audio(entree)
            if not audio:
                journal(f"  pas de fichier audio pour « {titre} » ; ignoré.")
                deja.add(ident)
                continue

            date = entree.get("published_parsed") or entree.get("updated_parsed")
            prefixe = (
                time.strftime("%Y-%m-%d", date)
                if date
                else datetime.now(timezone.utc).strftime("%Y-%m-%d")
            )
            base = f"{prefixe}-{slug(titre)}"

            journal(f"  téléchargement : {titre}")
            chemin_mp3 = RACINE / "episode_temp.mp3"
            try:
                with requests.get(audio, stream=True, timeout=300) as r:
                    r.raise_for_status()
                    with open(chemin_mp3, "wb") as f:
                        for bloc in r.iter_content(1 << 20):
                            f.write(bloc)

                verbatim = transcrire(chemin_mp3)

                dossier_v = DOSSIER_VERBATIMS / nom
                dossier_v.mkdir(parents=True, exist_ok=True)
                entete = (
                    f"# {titre}\n\nÉmission : {nom} — Date : {prefixe} — "
                    f"[Page de l'épisode]({entree.get('link', audio)})\n\n"
                )
                lien_vers_syn = (
                    f"➡️ **[Lire la synthèse](../../syntheses/{nom}/{base}.md)**"
                    "\n\n---\n\n"
                )
                lien_vers_verb = (
                    f"➡️ **[Lire le verbatim intégral]"
                    f"(../../verbatims/{nom}/{base}.md)**\n\n---\n\n"
                )
                (dossier_v / f"{base}.md").write_text(
                    entete + lien_vers_syn + verbatim, encoding="utf-8"
                )
                journal(f"  verbatim écrit : verbatims/{nom}/{base}.md")

                try:
                    syn = synthetiser(verbatim, titre, nom)
                    dossier_s = DOSSIER_SYNTHESES / nom
                    dossier_s.mkdir(parents=True, exist_ok=True)
                    (dossier_s / f"{base}.md").write_text(
                        entete + lien_vers_verb + syn, encoding="utf-8"
                    )
                    journal(f"  synthèse écrite : syntheses/{nom}/{base}.md")
                except Exception as e:  # noqa: BLE001
                    journal(f"  synthèse impossible ({e}) ; le verbatim est conservé.")

                deja.add(ident)
                traites_total += 1
                etat[nom] = sorted(deja)[-300:]  # borne la taille de l'état
                sauver_etat(etat)
            except Exception as e:  # noqa: BLE001
                journal(f"  échec sur cet épisode : {e}")
            finally:
                chemin_mp3.unlink(missing_ok=True)

    generer_sommaire()
    generer_flux_syntheses()
    journal(f"Terminé : {traites_total} épisode(s) traité(s) durant cette exécution.")


def generer_flux_syntheses() -> None:
    """Produit flux-syntheses.xml : un flux RSS contenant le texte intégral
    des synthèses, auquel on peut s'abonner dans Inoreader ou tout lecteur."""
    from xml.sax.saxutils import escape

    depot = os.environ.get("GITHUB_REPOSITORY", "utilisateur/depot")
    base_url = f"https://github.com/{depot}/blob/main"

    elements = []
    for fichier in DOSSIER_SYNTHESES.glob("*/*.md"):
        emission = fichier.parent.name
        base = fichier.stem
        date = base[:10]
        texte = fichier.read_text(encoding="utf-8")
        titre_ligne = texte.splitlines()[0].lstrip("# ").strip() if texte else base
        corps = texte.split("---", 1)[-1].strip()
        lien = f"{base_url}/syntheses/{emission}/{fichier.name}"
        elements.append(
            (
                date,
                "<item>"
                f"<title>{escape(f'[{emission}] {titre_ligne}')}</title>"
                f"<link>{escape(lien)}</link>"
                f"<guid isPermaLink='false'>{escape(f'{emission}/{base}')}</guid>"
                f"<pubDate>{date}</pubDate>"
                f"<description>{escape(corps)}</description>"
                "</item>",
            )
        )
    elements.sort(key=lambda x: x[0], reverse=True)
    xml = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<rss version='2.0'><channel>"
        "<title>Synthèses de podcasts</title>"
        f"<link>https://github.com/{depot}</link>"
        "<description>Synthèses automatiques — le novateur, sans le "
        "remplissage</description>"
        + "".join(e for _, e in elements[:100])
        + "</channel></rss>"
    )
    (RACINE / "flux-syntheses.xml").write_text(xml, encoding="utf-8")
    journal("Flux RSS des synthèses régénéré (flux-syntheses.xml).")


def generer_sommaire() -> None:
    """Reconstruit INDEX.md : tous les épisodes, du plus récent au plus ancien,
    avec lien direct vers la synthèse et vers le verbatim."""
    lignes = []
    for fichier in DOSSIER_VERBATIMS.glob("*/*.md"):
        emission = fichier.parent.name
        base = fichier.stem
        date = base[:10]
        titre = base[11:].replace("-", " ")
        syn = DOSSIER_SYNTHESES / emission / fichier.name
        lien_syn = (
            f"[synthèse](syntheses/{emission}/{fichier.name})" if syn.exists() else "—"
        )
        lignes.append(
            (
                date,
                f"| {date} | {emission} | {titre} | {lien_syn} | "
                f"[verbatim](verbatims/{emission}/{fichier.name}) |",
            )
        )
    lignes.sort(key=lambda x: x[0], reverse=True)
    contenu = (
        "# Sommaire des épisodes\n\n"
        "| Date | Émission | Épisode | Synthèse | Verbatim |\n"
        "|---|---|---|---|---|\n" + "\n".join(l for _, l in lignes) + "\n"
    )
    (RACINE / "INDEX.md").write_text(contenu, encoding="utf-8")
    journal(f"Sommaire régénéré ({len(lignes)} épisodes).")


if __name__ == "__main__":
    sys.exit(principal())
