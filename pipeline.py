#!/usr/bin/env python3
"""
Chaîne de veille podcasts :
  flux RSS -> téléchargement MP3 -> transcription (faster-whisper)
           -> verbatim (verbatims/) -> synthèse via l'API Gemini (syntheses/)

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

# Modèle de transcription : "small" = bon compromis vitesse/qualité.
# Passer à "medium" pour plus de fidélité (plus lent), "base" pour plus de débit.
MODELE_WHISPER = os.environ.get("MODELE_WHISPER", "small")

# À la découverte d'un flux, on n'aspire pas tout l'historique :
# seuls les N épisodes les plus récents sont traités d'emblée.
EPISODES_INITIAUX_PAR_FLUX = int(os.environ.get("EPISODES_INITIAUX", "5"))

# Aux passages suivants, seuls les épisodes récents sont examinés (fenêtre
# glissante) ; les archives ne sont traitées que sur demande explicite via
# le fichier rattrapage.txt (lignes : NomFlux | fragment du titre).
FENETRE_COURANTE = 15
FICHIER_RATTRAPAGE = RACINE / "rattrapage.txt"


def lire_rattrapage() -> dict[str, list[str]]:
    demandes: dict[str, list[str]] = {}
    if FICHIER_RATTRAPAGE.exists():
        for ligne in FICHIER_RATTRAPAGE.read_text(encoding="utf-8").splitlines():
            ligne = ligne.strip()
            if not ligne or ligne.startswith("#") or "|" not in ligne:
                continue
            nom, fragment = (p.strip() for p in ligne.split("|", 1))
            demandes.setdefault(slug(nom, 40), []).append(fragment.lower())
    return demandes

# Synthèse via l'API Gemini de Google (niveau gratuit, clé dans GEMINI_API_KEY).
# Cascade de noms de modèles : l'alias glissant d'abord (survit aux retraits),
# puis des noms datés en secours ; bascule automatique sur 404.
MODELES_SYNTHESE = [
    m for m in [
        os.environ.get("MODELE_SYNTHESE"),
        "gemini-flash-latest",
        "gemini-3.7-flash",
        "gemini-2.5-flash",
    ] if m
]
_modele_actif = 0

# Disjoncteur : passe à True quand le quota du jour est épuisé ; le passage
# s'arrête alors proprement au lieu de s'obstiner appel après appel.
quota_epuise = False

# Budget temps du passage (minutes) : au-delà, on ne commence plus d'épisode,
# pour laisser à la sauvegarde le temps de s'exécuter avant le timeout.
LIMITE_MINUTES = int(os.environ.get("LIMITE_MINUTES", "240"))
DEBUT_RUN = time.monotonic()


def temps_ecoule() -> bool:
    return time.monotonic() - DEBUT_RUN > LIMITE_MINUTES * 60


URL_MODELS = (
    "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
)

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
    texte = unicodedata.normalize("NFKC", texte)
    texte = re.sub(r"[^\w]+", "-", texte).strip("-")
    return texte[:longueur].rstrip("-") or "sans-titre"


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
        if chemin.name == "flux-syntheses.xml":
            continue
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
    # Regroupement des segments en paragraphes (~600 caractères), pour un
    # verbatim lisible et des citations exactes retrouvables d'un seul tenant.
    paragraphes, courant, taille = [], [], 0
    for seg in segments:
        courant.append(seg.text.strip())
        taille += len(seg.text)
        if taille > 600:
            paragraphes.append(" ".join(courant))
            courant, taille = [], 0
    if courant:
        paragraphes.append(" ".join(courant))
    journal(f"Transcrit ({info.language}, {info.duration/60:.0f} min d'audio).")
    return "\n\n".join(paragraphes)


# ---------------------------------------------------------------- synthèse


def decouper(texte: str, taille: int = 120000) -> list[str]:
    return [texte[i : i + taille] for i in range(0, len(texte), taille)]


def appel_modele(messages: list[dict], max_sortie: int = 2000) -> str:
    global _modele_actif, quota_epuise
    if quota_epuise:
        raise RuntimeError("quota du jour épuisé — appel non tenté")
    jeton = os.environ.get("GEMINI_API_KEY")
    if not jeton:
        raise RuntimeError(
            "GEMINI_API_KEY absente (à créer dans Settings > Secrets) : "
            "synthèse impossible."
        )
    tentatives = 0
    while tentatives < 4:
        modele = MODELES_SYNTHESE[_modele_actif]
        r = requests.post(
            URL_MODELS,
            headers={
                "Authorization": f"Bearer {jeton}",
                "Content-Type": "application/json",
            },
            json={
                "model": modele,
                "messages": messages,
                "temperature": 0.3,
                "max_tokens": max_sortie,
            },
            timeout=300,
        )
        if r.status_code == 404 and _modele_actif < len(MODELES_SYNTHESE) - 1:
            journal(
                f"Modèle « {modele} » introuvable (404) ; bascule sur "
                f"« {MODELES_SYNTHESE[_modele_actif + 1]} »."
            )
            _modele_actif += 1
            continue
        if r.status_code in (429, 503):  # limite de débit : on patiente
            motif = " ".join(r.text[:400].split())
            if tentatives == 0:
                journal(f"Limite de débit — réponse du service : {motif}")
            if "PerDay" in r.text or "per day" in r.text.lower():
                quota_epuise = True
                journal("Limite QUOTIDIENNE atteinte : arrêt des appels pour ce passage.")
                raise RuntimeError("quota quotidien épuisé")
            tentatives += 1
            attente = 40 * tentatives
            journal(f"Limite de débit ; pause {attente} s.")
            time.sleep(attente)
            continue
        if not r.ok:
            corps = " ".join(r.text[:300].split())
            raise RuntimeError(f"{r.status_code} — {corps}")
        time.sleep(7)  # niveau gratuit : ~10 requêtes/minute
        return r.json()["choices"][0]["message"]["content"]
    quota_epuise = True
    journal(
        "Quota Gemini du jour vraisemblablement épuisé : les synthèses "
        "restantes attendront un passage ultérieur."
    )
    raise RuntimeError("quota/limites de débit épuisés")


# Mettre à "0" dans le workflow pour désactiver la mise au propre du verbatim
STRUCTURER = os.environ.get("STRUCTURER", "1") == "1"


def structurer_verbatim(verbatim: str) -> str:
    """Édition légère du verbatim par le modèle : correction des coquilles de
    transcription et de la ponctuation, suppression des hésitations et faux
    départs, intertitres (##) et passages saillants en gras. Interdiction de
    résumer : tout le contenu est conservé. Si un fragment réécrit est
    anormalement court (contenu perdu), l'original est gardé."""
    fragments = []
    morceaux = decouper(verbatim, 20000)
    for i, morceau in enumerate(morceaux, 1):
        if quota_epuise:
            fragments.append(morceau)
            continue
        if len(morceaux) > 1:
            journal(f"  mise au propre du fragment {i}/{len(morceaux)}…")
        prompt = (
            "Voici un fragment de transcription automatique de podcast. "
            "Rends-le lisible SANS RIEN RÉSUMER NI OMETTRE :\n"
            "- corrige les erreurs manifestes de transcription (noms propres, "
            "mots déformés) et la ponctuation ;\n"
            "- supprime uniquement les hésitations, répétitions accidentelles "
            "et faux départs ;\n"
            "- conserve la langue d'origine, le style oral et TOUT le contenu "
            "(chaque idée, chiffre, exemple, digression) ;\n"
            "- structure en paragraphes, avec 2 à 5 intertitres thématiques "
            "en lignes '## Titre' ;\n"
            "- mets en gras (**…**) les passages les plus importants ou "
            "novateurs : thèses fortes, chiffres, annonces, désaccords.\n"
            "Réponds UNIQUEMENT par le texte mis au propre, sans préambule ni "
            "commentaire.\n\nFragment :\n" + morceau
        )
        try:
            propre = appel_modele(
                [{"role": "user", "content": prompt}], max_sortie=16000
            ).strip()
        except Exception as e:  # noqa: BLE001
            journal(f"  mise au propre du fragment {i} impossible ({e}).")
            fragments.append(morceau)
            continue
        # Garde-fou anti-condensation : un toilettage ne raccourcit guère.
        if len(propre) < 0.55 * len(morceau):
            journal(
                f"  fragment {i} : réécriture trop courte "
                f"({len(propre)}/{len(morceau)} car.), original conservé."
            )
            fragments.append(morceau)
        else:
            fragments.append(propre)
    return "\n\n".join(fragments)


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
    rattrapage = lire_rattrapage()
    traites_total = 0

    for nom, url in lire_flux():
        if traites_total >= MAX_EPISODES_PAR_RUN or quota_epuise or temps_ecoule():
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
        fenetre = (
            flux.entries[:EPISODES_INITIAUX_PAR_FLUX]
            if premiere_fois
            else flux.entries[:FENETRE_COURANTE]
        )

        # Rattrapage d'archives : épisodes anciens explicitement demandés,
        # par fragment de titre (« sleep toolkit ») ou par nombre
        # (« derniers:5 » = les 5 plus récents, déjà faits exclus).
        forces_ids = set()
        entrees = list(fenetre)
        fragments = rattrapage.get(nom, [])
        if fragments:
            candidats = []
            for f in fragments:
                m = re.fullmatch(r"derniers?\s*:\s*(\d+)", f)
                if m:
                    candidats.extend(flux.entries[: int(m.group(1))])
                else:
                    candidats.extend(
                        e for e in flux.entries
                        if f in (e.get("title") or "").lower()
                    )
            for entree in candidats:
                ident_e = (
                    entree.get("id") or entree.get("link")
                    or entree.get("title", "")
                )
                forces_ids.add(ident_e)
                if entree not in entrees:
                    entrees.append(entree)

        for entree in entrees:
            if traites_total >= MAX_EPISODES_PAR_RUN or quota_epuise or temps_ecoule():
                if temps_ecoule():
                    journal("Budget temps du passage atteint ; on s'arrête proprement.")
                break
            ident = entree.get("id") or entree.get("link") or entree.get("title", "")
            force = ident in forces_ids
            if not ident or (ident in deja and not force):
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
            if force and (DOSSIER_VERBATIMS / nom / f"{base}.md").exists():
                continue  # rattrapage déjà servi

            journal(f"  téléchargement : {titre}")
            chemin_mp3 = RACINE / "episode_temp.mp3"
            try:
                with requests.get(audio, stream=True, timeout=300) as r:
                    r.raise_for_status()
                    with open(chemin_mp3, "wb") as f:
                        for bloc in r.iter_content(1 << 20):
                            f.write(bloc)

                verbatim = transcrire(chemin_mp3)

                # 1. Synthèse complète (fichier séparé) — peut échouer sans
                #    bloquer le reste.
                syn = None
                try:
                    syn = synthetiser(verbatim, titre, nom)
                except Exception as e:  # noqa: BLE001
                    journal(f"  synthèse impossible ({e}) ; le verbatim est conservé.")

                # 2. Structuration du verbatim (intertitres + gras), texte intact.
                corps_verbatim = verbatim
                if STRUCTURER:
                    try:
                        corps_verbatim = structurer_verbatim(verbatim)
                    except Exception as e:  # noqa: BLE001
                        journal(f"  structuration impossible ({e}) ; verbatim brut.")

                # 3. Chapeau « L'essentiel » : premier bloc de la synthèse.
                essentiel = syn.split("\n\n")[0].strip() if syn else ""
                bloc_essentiel = (
                    f"### L'essentiel\n\n{essentiel}\n\n---\n\n" if essentiel else ""
                )

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
                    entete + lien_vers_syn + bloc_essentiel + corps_verbatim,
                    encoding="utf-8",
                )
                journal(f"  verbatim écrit : verbatims/{nom}/{base}.md")

                if syn:
                    dossier_s = DOSSIER_SYNTHESES / nom
                    dossier_s.mkdir(parents=True, exist_ok=True)
                    (dossier_s / f"{base}.md").write_text(
                        entete + lien_vers_verb + syn, encoding="utf-8"
                    )
                    journal(f"  synthèse écrite : syntheses/{nom}/{base}.md")

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


def _markdown_vers_html(md: str) -> str:
    """Conversion minimale Markdown -> HTML pour le rendu dans les lecteurs
    RSS : titres, gras, italique, listes, paragraphes."""
    from xml.sax.saxutils import escape as esc

    blocs_html = []
    for bloc in re.split(r"\n\s*\n", md.strip()):
        b = bloc.strip()
        if not b:
            continue
        if b.startswith("### "):
            blocs_html.append(f"<h4>{esc(b[4:])}</h4>")
            continue
        if b.startswith("## "):
            blocs_html.append(f"<h3>{esc(b[3:])}</h3>")
            continue
        if b.startswith("# "):
            blocs_html.append(f"<h2>{esc(b[2:])}</h2>")
            continue
        lignes = b.splitlines()
        if all(re.match(r"^\s*[-*•]\s+", l) for l in lignes):
            items = "".join(
                f"<li>{esc(re.sub(chr(94) + r'[-*•\s]+', '', l))}</li>" for l in lignes
            )
            blocs_html.append(f"<ul>{items}</ul>")
            continue
        blocs_html.append(f"<p>{esc(b)}</p>")
    html = "\n".join(blocs_html)
    # Gras et italique (après échappement, les ** et * sont intacts)
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html, flags=re.S)
    html = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", html, flags=re.S)
    return html


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
        corps_html = _markdown_vers_html(corps)
        lien = f"{base_url}/syntheses/{emission}/{fichier.name}"
        elements.append(
            (
                date,
                "<item>"
                f"<title>{escape(f'[{emission}] {titre_ligne}')}</title>"
                f"<link>{escape(lien)}</link>"
                f"<guid isPermaLink='false'>{escape(f'{emission}/{base}')}</guid>"
                f"<pubDate>{date}</pubDate>"
                f"<description>{escape(corps_html)}</description>"
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


if __name__ == "__main__":
    sys.exit(principal())
