import html
import json
import re
from typing import Any
from urllib.parse import urlparse

from app.models import Household, Item, Recipe
from app.service.ingredient_parsing import parseIngredients

KNOWN_CONNECTORS: list[tuple[str, str]] = [
    ("ricardocuisine.com", "ricardo"),
    ("marmiton.org", "marmiton"),
    ("app.kitchenowl.org", "kitchenowl"),
]


def detectConnector(url: str) -> str:
    parsed = urlparse(url if url.startswith("http") else f"https://{url}")
    host = (parsed.netloc or "").lower()
    for domain, connector_name in KNOWN_CONNECTORS:
        if host == domain or host.endswith("." + domain):
            return connector_name
    if url.startswith("kitchenowl://"):
        return "kitchenowl"
    return "generic"


def scrapeWithConnector(
    url: str, page_html: str, household: Household
) -> dict[str, Any] | None:
    connector = detectConnector(url)
    handler = {
        "ricardo": scrapeRicardo,
        "marmiton": scrapeMarmiton,
    }.get(connector)
    if handler is None:
        return None
    return handler(url, page_html, household)


def scrapeRicardo(url: str, page_html: str, household: Household) -> dict[str, Any] | None:
    recipe_json = _find_recipe_json_ld(page_html)
    if recipe_json is None:
        return None

    tags = _split_keywords(recipe_json.get("keywords"))
    for tag in _coerce_string_list(recipe_json.get("tags")):
        label = _humanize_ricardo_tag(tag)
        if label:
            tags.append(label)

    for category in _coerce_string_list(recipe_json.get("recipeCategory")):
        tags.append(category)

    return _build_scrape_response(
        connector_name="ricardo",
        url=url,
        household=household,
        recipe_json=recipe_json,
        tags=tags,
    )


def scrapeMarmiton(
    url: str, page_html: str, household: Household
) -> dict[str, Any] | None:
    recipe_json = _find_recipe_json_ld(page_html)
    if recipe_json is None:
        return None

    tags = _split_keywords(recipe_json.get("keywords"))
    for category in _coerce_string_list(recipe_json.get("recipeCuisine")):
        tags.append(category)
    for category in _coerce_string_list(recipe_json.get("recipeCategory")):
        tags.append(category)

    return _build_scrape_response(
        connector_name="marmiton",
        url=url,
        household=household,
        recipe_json=recipe_json,
        tags=tags,
    )


def _build_scrape_response(
    connector_name: str,
    url: str,
    household: Household,
    recipe_json: dict[str, Any],
    tags: list[str] | None = None,
) -> dict[str, Any] | None:
    title = _clean_text(recipe_json.get("name"))
    ingredient_lines = _coerce_string_list(recipe_json.get("recipeIngredient"))
    if not title or not ingredient_lines:
        return None

    recipe = Recipe()
    recipe.name = title[:128]
    recipe.prep_time = _parse_minutes(recipe_json.get("prepTime")) or 0
    recipe.cook_time = _parse_minutes(recipe_json.get("cookTime")) or 0
    recipe.time = _parse_minutes(recipe_json.get("totalTime")) or (
        recipe.prep_time + recipe.cook_time
    )
    recipe.yields = _parse_yields(recipe_json.get("recipeYield")) or 0
    recipe.photo = _extract_image_url(recipe_json.get("image"))
    recipe.source = url

    description_parts = [
        _clean_text(recipe_json.get("description")),
        _parse_instructions(recipe_json.get("recipeInstructions")),
    ]
    recipe.description = "\n\n".join(part for part in description_parts if part)

    items = _build_items(ingredient_lines, household)

    recipe_payload = recipe.obj_to_dict()
    tag_payload = _build_tag_payload(tags or [])
    if tag_payload:
        recipe_payload["tags"] = tag_payload

    return {
        "recipe": recipe_payload,
        "items": items,
        "connector": connector_name,
    }


def _build_items(ingredient_lines: list[str], household: Household) -> dict[str, Any]:
    items: dict[str, Any] = {}
    for ingredient in parseIngredients(ingredient_lines, household.language):
        original = ingredient.originalText or ingredient.name or ""
        if not original:
            continue
        name = ingredient.name if ingredient.name else original
        item = Item.find_name_starts_with(household.id, name)
        if item:
            items[original] = item.obj_to_dict() | {
                "description": ingredient.description,
                "optional": False,
            }
        else:
            items[original] = None
    return items


def _build_tag_payload(tags: list[str]) -> list[dict[str, str]]:
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for tag in tags:
        clean = _clean_text(tag)
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": clean})
    return out


def _find_recipe_json_ld(page_html: str) -> dict[str, Any] | None:
    pattern = re.compile(
        r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(page_html):
        raw = html.unescape(match.group(1).strip())
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for candidate in _iter_json_ld_nodes(parsed):
            if _is_recipe_node(candidate):
                return candidate
    return None


def _iter_json_ld_nodes(node: Any):
    if isinstance(node, list):
        for item in node:
            yield from _iter_json_ld_nodes(item)
        return
    if not isinstance(node, dict):
        return
    yield node
    graph = node.get("@graph")
    if isinstance(graph, list):
        for item in graph:
            yield from _iter_json_ld_nodes(item)


def _is_recipe_node(node: dict[str, Any]) -> bool:
    node_type = node.get("@type")
    if isinstance(node_type, list):
        return any(_type_is_recipe(value) for value in node_type)
    return _type_is_recipe(node_type)


def _type_is_recipe(value: Any) -> bool:
    return isinstance(value, str) and value.lower() == "recipe"


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        text = str(value)
    text = html.unescape(text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\r", "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _parse_minutes(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if not isinstance(value, str):
        return None

    iso_match = re.fullmatch(r"P(?:T(?:(\d+)H)?(?:(\d+)M)?)", value.strip())
    if iso_match:
        hours = int(iso_match.group(1) or 0)
        minutes = int(iso_match.group(2) or 0)
        return hours * 60 + minutes

    digit_match = re.search(r"\d+", value)
    if digit_match:
        return int(digit_match.group(0))
    return None


def _parse_yields(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, list):
        for item in value:
            parsed = _parse_yields(item)
            if parsed is not None:
                return parsed
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        match = re.search(r"\d+", value)
        if match:
            return int(match.group(0))
    return None


def _parse_instructions(value: Any) -> str:
    lines = _collect_instruction_lines(value)
    return "\n".join(line for line in lines if line)


def _collect_instruction_lines(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = _clean_text(value)
        return [text] if text else []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_collect_instruction_lines(item))
        return out
    if not isinstance(value, dict):
        return []

    section_name = _clean_text(value.get("name"))
    text = _clean_text(value.get("text"))
    nested = _collect_instruction_lines(value.get("itemListElement"))

    if nested:
        if section_name:
            return [section_name + ":"] + nested
        return nested
    if text:
        return [text]
    if section_name:
        return [section_name]
    return []


def _coerce_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        clean = _clean_text(value)
        return [clean] if clean else []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if isinstance(item, str):
                clean = _clean_text(item)
                if clean:
                    out.append(clean)
            elif isinstance(item, dict):
                for key in ("name", "text"):
                    clean = _clean_text(item.get(key))
                    if clean:
                        out.append(clean)
                        break
        return out
    return []


def _split_keywords(value: Any) -> list[str]:
    if isinstance(value, list):
        parts = []
        for item in value:
            parts.extend(_split_keywords(item))
        return parts
    if not isinstance(value, str):
        return []
    return [_clean_text(part) for part in value.split(",") if _clean_text(part)]


def _extract_image_url(value: Any) -> str | None:
    if isinstance(value, str):
        clean = value.strip()
        return clean or None
    if isinstance(value, list):
        for item in value:
            extracted = _extract_image_url(item)
            if extracted:
                return extracted
        return None
    if isinstance(value, dict):
        for key in ("url", "contentUrl", "@id"):
            extracted = _extract_image_url(value.get(key))
            if extracted:
                return extracted
    return None


def _humanize_ricardo_tag(tag: str) -> str:
    tag_map = {
        "diet_dairy-free-diet": "Sans produits laitiers",
        "diet_egg-free-diet": "Sans oeufs",
        "diet_gluten-free-diet": "Sans gluten",
        "diet_lactose-free-diet": "Sans lactose",
        "diet_nut-free-diet": "Sans noix",
        "diet_groundnut-free-diet": "Sans arachides",
        "diet_vegetarian-diet": "Végétarien",
        "diet_vegan-diet": "Végétalien",
        "theme_freezer-friendly": "Se congèle bien",
        "theme_one-pot": "One pot",
        "theme_quick-and-easy": "Rapide et facile",
        "theme_budget-friendly": "Petit budget",
        "theme_meal-prep": "Meal prep",
    }
    return tag_map.get(tag, "")