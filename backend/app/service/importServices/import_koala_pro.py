import re
from typing import Any

from app.models import Household
from app.service.ingredient_parsing import parseIngredients

from .import_recipe import importRecipe


def _parse_int(value: Any) -> int | None:
    if value is None:
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


def _element_names(elements: Any) -> list[str]:
    if not isinstance(elements, list):
        return []
    names: list[str] = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        name = str(element.get("name") or "").strip()
        if name:
            names.append(name)
    return names


def _split_ingredient_lines(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        clean = line.strip().strip("-*")
        if clean:
            out.append(clean)
    return out


def _parse_ingredient_items(
    ingredient_lines: list[str], language: str | None
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    def append_item(name: str, description: str = ""):
        clean_name = name.strip()
        if not clean_name:
            return
        items.append(
            {
                "name": clean_name,
                "description": description.strip(),
                "optional": False,
            }
        )

    try:
        parsed_items = parseIngredients(ingredient_lines, language)
    except Exception:
        parsed_items = []

    if parsed_items:
        for parsed in parsed_items:
            append_item(parsed.name or parsed.originalText or "", parsed.description or "")
        return items

    for ingredient_line in ingredient_lines:
        try:
            parsed = parseIngredients([ingredient_line], language)
        except Exception:
            parsed = []

        if parsed:
            append_item(
                parsed[0].name or parsed[0].originalText or "",
                parsed[0].description or "",
            )
            continue

        append_item(ingredient_line)

    return items


def importKoalaProRecipes(
    household: Household,
    koala_payload: list[dict[str, Any]],
    overwrite: bool = False,
) -> int:
    imported = 0

    for wrapper in koala_payload:
        if not isinstance(wrapper, dict):
            continue

        raw: dict[str, Any] = {}
        if isinstance(wrapper.get("fr"), dict):
            raw = wrapper["fr"]
        elif isinstance(wrapper.get("en"), dict):
            raw = wrapper["en"]
        else:
            raw = wrapper

        title = str(raw.get("title") or "").strip()
        if not title:
            continue

        ingredients_text = str(raw.get("field_ingredients_descrip") or "").strip()
        ingredient_lines = _split_ingredient_lines(ingredients_text)

        for ingredient_name in _element_names(raw.get("field_recipe_ingredients")):
            if ingredient_name.lower() not in [e.lower() for e in ingredient_lines]:
                ingredient_lines.append(ingredient_name)

        items = _parse_ingredient_items(ingredient_lines, household.language)

        tags: list[str] = []
        tags.extend(_element_names(raw.get("field_recipe_particularites")))
        tags.extend(_element_names(raw.get("field_recipe_themes")))
        tags.extend(_element_names(raw.get("field_recipe_menus")))
        tags.extend(_element_names(raw.get("field_recipe_type_de_plat")))

        description_parts = [
            str(raw.get("content") or "").strip(),
            str(raw.get("field_preparation_description") or "").strip(),
        ]
        description = "\n\n".join(part for part in description_parts if part)

        source_suffix = str(raw.get("field_nid") or "").strip()
        source = "koala-pro"
        if source_suffix:
            source = f"koala-pro://{source_suffix}"

        importRecipe(
            household.id,
            {
                "name": title,
                "description": description,
                "time": _parse_int(raw.get("field_preparation")),
                "prep_time": _parse_int(raw.get("field_preparation")),
                "cook_time": _parse_int(raw.get("field_cooking")),
                "yields": _parse_int(raw.get("field_portion")),
                "source": source,
                "items": items,
                "tags": list(dict.fromkeys([tag for tag in tags if tag.strip()])),
            },
            overwrite,
        )
        imported += 1

    return imported