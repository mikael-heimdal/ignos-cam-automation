#!/usr/bin/env python3

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import request, error


BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "config" / "config.json"
STATE_FILE = BASE_DIR / "state" / "seen_parts.json"
OUTPUT_DIR = BASE_DIR / "output" / "parts"


def load_json(path, default=None):
    if not path.exists():
        return default

    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary_file = path.with_suffix(path.suffix + ".tmp")

    with temporary_file.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)

    temporary_file.replace(path)


def api_request(config, method, endpoint, body=None):
    base_url = config["base_url"].rstrip("/")
    url = f"{base_url}{endpoint}"

    token_environment = config.get(
        "token_environment",
        "IGNOS_API_TOKEN"
    )

    token = os.getenv(token_environment)

    if not token:
        raise RuntimeError(
            f"Miljövariabeln {token_environment} saknas."
        )

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}"
    }

    encoded_body = None

    if body is not None:
        headers["Content-Type"] = "application/json"
        encoded_body = json.dumps(body).encode("utf-8")

    api_call = request.Request(
        url=url,
        data=encoded_body,
        headers=headers,
        method=method
    )

    try:
        with request.urlopen(
            api_call,
            timeout=config.get("request_timeout_seconds", 30)
        ) as response:

            response_text = response.read().decode("utf-8")

            if not response_text:
                return None

            return json.loads(response_text)

    except error.HTTPError as exception:
        response_text = exception.read().decode(
            "utf-8",
            errors="replace"
        )

        raise RuntimeError(
            f"Ignos svarade HTTP {exception.code}: "
            f"{response_text[:500]}"
        ) from exception

    except error.URLError as exception:
        raise RuntimeError(
            f"Kunde inte ansluta till Ignos: "
            f"{exception.reason}"
        ) from exception


def extract_parts(api_response, response_items_path=""):
    current = api_response

    if response_items_path:
        for key in response_items_path.split("."):
            if not isinstance(current, dict):
                return []

            current = current.get(key)

    if isinstance(current, list):
        return current

    if isinstance(current, dict):
        for possible_key in ("items", "results", "value", "data"):
            possible_items = current.get(possible_key)

            if isinstance(possible_items, list):
                return possible_items

    raise RuntimeError(
        "Kunde inte hitta detaljlistan i API-svaret. "
        "Kontrollera response_items_path i config.json."
    )


def get_parts(config):
    endpoint = config.get(
        "parts_list_path",
        "/cncsetup/parts/list"
    )

    request_body = config.get(
        "parts_list_body",
        {}
    )

    response = api_request(
        config=config,
        method="POST",
        endpoint=endpoint,
        body=request_body
    )

    return extract_parts(
        response,
        config.get("response_items_path", "")
    )


def get_part_details(config, part_id):
    endpoint_template = config.get(
        "part_detail_path",
        "/cncsetup/parts/{id}"
    )

    endpoint = endpoint_template.replace(
        "{id}",
        str(part_id)
    )

    return api_request(
        config=config,
        method="GET",
        endpoint=endpoint
    )


def normalize_part(part):
    part_number = (
        part.get("partNumber")
        or part.get("partName")
        or str(part.get("id"))
    )

    return {
        "id": part.get("id"),
        "partNumber": part.get("partNumber"),
        "partRevision": part.get("partRevision"),
        "partName": part.get("partName"),
        "customerId": part.get("customerId"),
        "customerName": part.get("customerName"),
        "material": part.get("material"),
        "drawing": part.get("drawing"),
        "drawingRevision": part.get("drawingRevision"),
        "description": part.get("description"),
        "machineGroup": part.get("machineGroup"),
        "templateType": "",
        "layerName": f"1.Part_{part_number}",
        "detectedAtUtc": datetime.now(
            timezone.utc
        ).isoformat()
    }


def safe_filename(value):
    value = str(value or "unknown")

    return "".join(
        character
        if character.isalnum() or character in "-_."
        else "_"
        for character in value
    )


def check_for_new_parts(config, bootstrap=False):
    state = load_json(
        STATE_FILE,
        {
            "seen_ids": [],
            "last_poll_utc": None
        }
    )

    seen_ids = {
        str(part_id)
        for part_id in state.get("seen_ids", [])
    }

    parts = get_parts(config)

    valid_parts = [
        part
        for part in parts
        if isinstance(part, dict)
        and part.get("id") is not None
    ]

    current_ids = {
        str(part["id"])
        for part in valid_parts
    }

    if bootstrap and not seen_ids:
        print(
            f"Bootstrap: registrerar "
            f"{len(current_ids)} befintliga detaljer."
        )

        new_parts = []

    else:
        new_parts = [
            part
            for part in valid_parts
            if str(part["id"]) not in seen_ids
        ]

    for part_summary in new_parts:
        part_id = part_summary["id"]

        if config.get("fetch_part_details", True):
            part_details = get_part_details(
                config,
                part_id
            )
        else:
            part_details = part_summary

        if not isinstance(part_details, dict):
            print(
                f"Hoppar över detalj {part_id}: "
                "ogiltigt API-svar."
            )
            continue

        combined_part = {
            **part_summary,
            **part_details
        }

        normalized_part = normalize_part(
            combined_part
        )

        part_number = (
            normalized_part["partNumber"]
            or part_id
        )

        output_file = OUTPUT_DIR / (
            f"{safe_filename(part_number)}_"
            f"{safe_filename(part_id)}.json"
        )

        save_json(
            output_file,
            normalized_part
        )

        print(
            f"Ny detalj sparad: {output_file}"
        )

    state["seen_ids"] = sorted(
        seen_ids | current_ids
    )

    state["last_poll_utc"] = datetime.now(
        timezone.utc
    ).isoformat()

    save_json(
        STATE_FILE,
        state
    )

    return len(new_parts)


def main():
    config = load_json(CONFIG_FILE)

    if config is None:
        raise SystemExit(
            "config/config.json saknas. "
            "Skapa filen från config.example.json."
        )

    polling_interval = max(
        10,
        int(config.get("poll_interval_seconds", 60))
    )

    bootstrap = "--bootstrap" in os.sys.argv
    run_once = "--once" in os.sys.argv

    while True:
        try:
            new_count = check_for_new_parts(
                config,
                bootstrap=bootstrap
            )

            print(
                f"Kontrollen klar. "
                f"Nya detaljer: {new_count}"
            )

        except Exception as exception:
            print(
                f"Kontrollen misslyckades: {exception}"
            )

        if run_once:
            break

        bootstrap = False
        time.sleep(polling_interval)


if __name__ == "__main__":
    main()
