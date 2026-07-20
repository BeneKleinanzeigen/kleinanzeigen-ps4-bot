import os
import json
import base64
from datetime import datetime, timezone, timedelta
import requests

KLAZ_KEY = os.environ["KLAZ_API_KEY"]
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT_RAW = os.environ["TELEGRAM_CHAT_ID_RAW"]
TG_CHAT_FILTERED = os.environ["TELEGRAM_CHAT_ID_FILTERED"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]

STATE_FILE = "state.json"
PRICELIST_FILE = "pricelist.json"
SEARCH_QUERY = "PS4"
MAX_IMAGES = 4
STATE_RETENTION_DAYS = 30

KLAZ_SEARCH_URL = "https://api.kleinanzeigen-agent.de/api/v2/kleinanzeigen/search"
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"
CLAUDE_API = "https://api.anthropic.com/v1/messages"


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def send_telegram(chat_id, text):
    resp = requests.post(
        f"{TG_API}/sendMessage",
        data={"chat_id": chat_id, "text": text, "disable_web_page_preview": False},
        timeout=20,
    )
    if not resp.ok:
        print("Telegram Fehler:", resp.status_code, resp.text)


def search_kleinanzeigen(query, page=0):
    params = {"q": query, "page": page, "size": 50, "picture_required": "true"}
    headers = {"klaz_key": KLAZ_KEY}
    resp = requests.get(KLAZ_SEARCH_URL, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def download_image_b64(url):
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        media_type = r.headers.get("Content-Type", "image/jpeg")
        if "image" not in media_type:
            media_type = "image/jpeg"
        return {
            "type": "base64",
            "media_type": media_type,
            "data": base64.b64encode(r.content).decode(),
        }
    except Exception as e:
        print("Bild-Download fehlgeschlagen:", url, e)
        return None


def analyze_ad(ad, pricelist):
    images = (ad.get("images") or [])[:MAX_IMAGES]
    content = []
    for img_url in images:
        b64 = download_image_b64(img_url)
        if b64:
            content.append({"type": "image", "source": b64})

    title = ad.get("title", "")
    description = ad.get("description", "") or ""
    price_amount = ad.get("price", {}).get("amount", 0) or 0

    prompt = f"""Du bewertest ein PS4-Inserat fuer einen Ankaeufer von Spielkonsolen.

Preisliste (was der Ankaeufer maximal zahlt):
- Konsole PS4 (FAT): {pricelist['console']['PS4']} EUR
- Konsole PS4 Slim: {pricelist['console']['PS4 Slim']} EUR
- Konsole PS4 Pro: {pricelist['console']['PS4 Pro']} EUR
  (Bedingung: nicht defekt, Laufwerk und alle Funktionen muessen laut Beschreibung funktionieren.
  Speichergroesse, Kabel und OVP sind irrelevant fuer den Preis.)
- Controller original schwarz: {pricelist['controller']['schwarz']} EUR
- Controller original farbig: {pricelist['controller']['farbig']} EUR
  (Nur ORIGINAL Sony-Controller zaehlen. Pruefe auf den Fotos wenn moeglich das PS-Logo auf der
  mittleren Taste. Bei Unsicherheit oder erkennbarem Nachbau: nicht mitrechnen und in der
  Begruendung erwaehnen.)
- Spiele (Blu-ray Disc, Titel egal): {pricelist['game_flat_price']} EUR pro Stueck

Titel: {title}
Beschreibung: {description}
Angebotspreis auf Kleinanzeigen: {price_amount} EUR

Analysiere die angehaengten Fotos und den Text. Bestimme:
1. Welche PS4-Variante (FAT/Slim/Pro) - bei Unklarheit die guenstigste plausible Annahme nehmen.
2. Anzahl und Farbe der Controller, und eine Einschaetzung ob sie original sind.
3. Anzahl sichtbarer bzw. im Text erwaehnter Spiele.
4. Hinweise die gegen "voll funktionsfaehig" sprechen (z.B. "Laufwerk liest nicht", "als defekt",
   "Bastlerguert").

Antworte NUR mit einem JSON-Objekt, exakt in diesem Format, ohne weiteren Text davor oder danach:
{{
  "variante": "FAT|Slim|Pro",
  "controller_anzahl": 0,
  "controller_wert": 0,
  "spiele_anzahl": 0,
  "spiele_wert": 0,
  "konsolen_wert": 0,
  "gesamtwert": 0,
  "verdacht_defekt": false,
  "begruendung": "kurze Erklaerung auf Deutsch"
}}
"""

    body = {
        "model": "claude-sonnet-5",
        "max_tokens": 800,
        "messages": [{"role": "user", "content": content + [{"type": "text", "text": prompt}]}],
    }
    headers = {
        "x-api-key": ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    resp = requests.post(CLAUDE_API, headers=headers, json=body, timeout=60)
    resp.raise_for_status()
    text = resp.json()["content"][0]["text"]
    start = text.find("{")
    end = text.rfind("}") + 1
    return json.loads(text[start:end])


def main():
    pricelist = load_json(PRICELIST_FILE, {})
    state = load_json(STATE_FILE, {"seen": {}})
    seen = state.get("seen", {})

    cutoff = datetime.now(timezone.utc) - timedelta(days=STATE_RETENTION_DAYS)
    seen = {
        k: v
        for k, v in seen.items()
        if datetime.fromisoformat(v) > cutoff
    }

    data = search_kleinanzeigen(SEARCH_QUERY)
    ads = data.get("data", {}).get("ads", [])

    new_ads = [ad for ad in ads if str(ad.get("ad_id")) not in seen]
    print(f"{len(ads)} Treffer insgesamt, {len(new_ads)} davon neu.")

    for ad in new_ads:
        ad_id = str(ad.get("ad_id"))
        title = ad.get("title", "")
        price_amount = ad.get("price", {}).get("amount", "?")
        url = ad.get("ad_url", "")
        location = ad.get("location", {}).get("name", "") if ad.get("location") else ""

        send_telegram(TG_CHAT_RAW, f"Neu: {title}\n{price_amount} EUR - {location}\n{url}")

        try:
            analysis = analyze_ad(ad, pricelist)
            asking = price_amount if isinstance(price_amount, (int, float)) else 0
            margin = analysis.get("gesamtwert", 0) - asking

            if margin >= pricelist.get("margin_threshold", 30):
                warn = ""
                if analysis.get("verdacht_defekt"):
                    warn = "\nACHTUNG: Verdacht auf Defekt laut Beschreibung!"
                msg = (
                    f"MARGE ca. {margin:.0f} EUR\n"
                    f"{title}\n"
                    f"Angebotspreis: {asking} EUR - Standort: {location}\n"
                    f"Geschaetzter Wert: {analysis.get('gesamtwert')} EUR "
                    f"(Konsole {analysis.get('variante')}: {analysis.get('konsolen_wert')} EUR, "
                    f"{analysis.get('controller_anzahl')} Controller: {analysis.get('controller_wert')} EUR, "
                    f"{analysis.get('spiele_anzahl')} Spiele: {analysis.get('spiele_wert')} EUR)\n"
                    f"Begruendung: {analysis.get('begruendung')}{warn}\n"
                    f"{url}"
                )
                send_telegram(TG_CHAT_FILTERED, msg)
        except Exception as e:
            print(f"Analyse fehlgeschlagen fuer Anzeige {ad_id}: {e}")

        seen[ad_id] = datetime.now(timezone.utc).isoformat()

    state["seen"] = seen
    save_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
