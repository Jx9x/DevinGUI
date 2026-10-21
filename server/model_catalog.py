"""Pure protobuf decoding and model-catalogue metadata conversion.

No cache files, registry state, or subprocesses are accessed here. Both cached
protobuf fields and the public CLI JSON catalogue retain their existing shapes.
"""

from __future__ import annotations

import re
import struct
from typing import Any, Iterator

_CACHE_PAYLOAD_MAX_BYTES = 8 * 1024 * 1024
_WIREFIELD_MAX = 100_000


def _pretty_name(model_id: str) -> str:
    parts = model_id.replace("-", " ").replace("_", " ").split()
    return " ".join(p.capitalize() for p in parts)


def _fixed32_to_float(value: int) -> float:
    """Convert a fixed32 protobuf value to IEEE 754 float."""
    return struct.unpack('<f', struct.pack('<I', value))[0]


def _bytes_to_str(value) -> str:
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    return str(value)


def _read_varint(data: bytes, i: int) -> tuple[int, int]:
    """Read a 64-bit-legal varint at ``data[i]`` and return ``(value, next)``."""
    val = shift = 0
    for _ in range(10):
        if i >= len(data):
            raise ValueError("unterminated protobuf varint")
        b = data[i]
        i += 1
        if shift == 63 and b > 1:
            raise ValueError("protobuf varint exceeds 64 bits")
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, i
        shift += 7
    raise ValueError("protobuf varint exceeds 10 bytes")


def _iter_wire_fields(data: bytes) -> Iterator[tuple[int, int, int | bytes]]:
    """Yield ``(field_number, wire_type, value)`` for a protobuf blob.

    Values are integers for varint/fixed32/fixed64, bytes for length-delimited
    fields. Direct decoding avoids per-message schema inference and copying
    that stalled the event loop with generic decoding.
    """
    if len(data) > _CACHE_PAYLOAD_MAX_BYTES:
        raise ValueError("protobuf payload exceeds the byte limit")
    i, n = 0, len(data)
    fields = 0
    while i < n:
        fields += 1
        if fields > _WIREFIELD_MAX:
            raise ValueError("protobuf field count exceeds the limit")
        tag, i = _read_varint(data, i)
        field, wire = tag >> 3, tag & 7
        if tag > 0xFFFFFFFF or not 1 <= field <= 0x1FFFFFFF:
            raise ValueError(f"invalid protobuf tag {tag:#x}")
        if wire == 0:  # varint
            val, i = _read_varint(data, i)
            yield field, wire, val
        elif wire == 2:  # length-delimited
            ln, i = _read_varint(data, i)
            if ln > n - i:
                raise ValueError("truncated length-delimited protobuf field")
            yield field, wire, data[i:i + ln]
            i += ln
        elif wire == 5:  # fixed32
            if i + 4 > n:
                raise ValueError("truncated fixed32 protobuf field")
            yield field, wire, struct.unpack_from('<I', data, i)[0]
            i += 4
        elif wire == 1:  # fixed64
            if i + 8 > n:
                raise ValueError("truncated fixed64 protobuf field")
            yield field, wire, struct.unpack_from('<Q', data, i)[0]
            i += 8
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")


def _wire_map(data: bytes) -> dict[int, list[tuple[int, Any]]]:
    """Group ``_iter_wire_fields`` output into ``{field: [(wire, value)]}``."""
    out: dict[int, list[tuple[int, Any]]] = {}
    for field, wire, value in _iter_wire_fields(data):
        out.setdefault(field, []).append((wire, value))
    return out


def _wire_last(fields: dict, num: int, wire: int) -> Any:
    """Return the last value matching ``num`` and ``wire``, or None if absent."""
    for w, v in reversed(fields.get(num, ())):
        if w == wire:
            return v
    return None


def _wire_submsg(fields: dict, num: int) -> dict | None:
    """Last length-delimited field ``num`` decoded as a nested wire map."""
    raw = _wire_last(fields, num, 2)
    if raw is None:
        return None
    try:
        return _wire_map(raw)
    except ValueError:
        return None


def _context_tag(context_window: int) -> str | None:
    if not context_window or context_window <= 0:
        return None
    if context_window >= 1_000_000:
        return "1M"
    return f"{context_window // 1000}K"


def _reasoning_key(value: str) -> str:
    """Normalize effort labels: No Thinking/None -> none, X-High/XHigh -> xhigh.

    Strip whitespace and lowercase other values.
    """
    v = value.strip().lower()
    if v in ("no thinking", "none"):
        return "none"
    if v in ("x-high", "xhigh"):
        return "xhigh"
    return v


def _decode_model_cache(data: bytes) -> dict:
    """Decode known model-cache fields into string-keyed dicts and repeated lists.

    Malformed top-level data or no groups: ``{}``.
    Malformed nested messages: skip.
    """
    try:
        top = _wire_map(data)
    except ValueError:
        return {}
    groups: list[dict] = []
    for wire, blob in top.get(1, []):
        if wire != 2:
            continue
        try:
            fields = _wire_map(blob)
        except ValueError:
            continue
        group: dict[str, Any] = {}
        label = _wire_last(fields, 1, 2)
        if label is not None:
            group["1"] = label
        cost = _wire_last(fields, 3, 5)
        if cost is not None:
            group["3"] = cost
        promo = _wire_submsg(fields, 19)
        if promo is not None:
            flag = _wire_last(promo, 1, 0)
            if flag is not None:
                group["19"] = {"1": flag}
        internal_id = _wire_last(fields, 22, 2)
        if internal_id is not None:
            group["22"] = internal_id
        config = _wire_submsg(fields, 23)
        if config is not None:
            c: dict[str, Any] = {}
            ctx = _wire_last(config, 4, 0)
            if ctx is not None:
                c["4"] = ctx
            caps = _wire_submsg(config, 6)
            if caps is not None:
                vision = _wire_last(caps, 11, 0)
                if vision is not None:
                    c["6"] = {"11": vision}
            max_out = _wire_last(config, 13, 0)
            if max_out is not None:
                c["13"] = max_out
            alias = _wire_last(config, 23, 2)
            if alias is not None:
                c["23"] = alias
            if c:
                group["23"] = c
        display = _wire_submsg(fields, 30)
        if display is not None:
            d: dict[str, Any] = {}
            name = _wire_last(display, 1, 2)
            if name is not None:
                d["1"] = name
            labels: list[dict] = []
            for lw, lblob in display.get(2, []):
                if lw != 2:
                    continue
                try:
                    lf = _wire_map(lblob)
                except ValueError:
                    continue
                entry: dict[str, Any] = {}
                lname = _wire_last(lf, 1, 2)
                if lname is not None:
                    entry["1"] = lname
                value_msg = _wire_submsg(lf, 2)
                if value_msg is not None:
                    inner: dict[str, Any] = {}
                    sval = _wire_last(value_msg, 2, 2)
                    if sval is not None:
                        inner["2"] = sval
                    ival = _wire_last(value_msg, 1, 0)
                    if ival is not None:
                        inner["1"] = ival
                    if inner:
                        entry["2"] = inner
                if entry:
                    labels.append(entry)
            if labels:
                d["2"] = labels
            if d:
                group["30"] = d
        recommended = _wire_last(fields, 31, 0)
        if recommended is not None:
            group["31"] = recommended
        prices: list[dict] = []
        for pw, pblob in fields.get(32, []):
            if pw != 2:
                continue
            try:
                pf = _wire_map(pblob)
            except ValueError:
                continue
            pentry: dict[str, Any] = {}
            component = _wire_last(pf, 1, 2)
            if component is not None:
                pentry["1"] = component
            price = _wire_last(pf, 2, 5)
            if price is not None:
                pentry["2"] = price
            if pentry:
                prices.append(pentry)
        if prices:
            group["32"] = prices
        groups.append(group)
    return {"1": groups} if groups else {}


def _model_cache_entries(message: dict) -> tuple[dict[str, dict], dict[str, str]]:
    """Build independent variant metadata and aliases from decoded cache fields."""
    result: dict[str, dict] = {}
    id_map: dict[str, str] = {}

    for group in message.get("1", []):
        if not isinstance(group, dict):
            continue

        # Config sub-message (field 23)
        config = group.get("23", {})
        if not isinstance(config, dict):
            config = {}

        # Internal ID (field 22) — the ID the CLI accepts via --model and
        # the one stored in team_settings / sessions DB.
        internal_id = _bytes_to_str(group.get("22", b""))
        if not internal_id:
            continue

        # CLI model alias (field 23.23) — shared across variants of the same
        # model family, used for grouping in the UI.
        model_alias = _bytes_to_str(config.get("23", b""))

        # Display name (field 30.1) — the base model name, shared by variants.
        display = group.get("30", {})
        if not isinstance(display, dict):
            display = {}
        base_display_name = _bytes_to_str(display.get("1", b""))

        group_label = _bytes_to_str(group.get("1", b""))

        # Vision is field 23.6.11, not 23.6.8 (also set on non-vision models).
        caps = config.get("6", {})
        if not isinstance(caps, dict):
            caps = {}
        supports_images = bool(caps.get("11", 0))

        # Cost (field 3 - float)
        cost = 0.0
        if "3" in group:
            try:
                cost = _fixed32_to_float(group["3"])
            except (struct.error, TypeError):
                pass

        # Field 19 marks promotions, not free models. Only promo models without
        # field 32 pricing are treated as free.
        promo = group.get("19", {})
        is_promo = isinstance(promo, dict) and bool(promo.get("1", 0))
        has_pricing = "32" in group and len(group.get("32", [])) > 0
        is_free = is_promo and not has_pricing

        # Field 32 repeats price components (Input, Cached input, Output).
        # Each component's field 2 is a fixed32 float in USD per million tokens.
        input_price: float | None = None
        cached_input_price: float | None = None
        output_price: float | None = None
        if has_pricing:
            for price_entry in group["32"]:
                if not isinstance(price_entry, dict):
                    continue
                component = _bytes_to_str(price_entry.get("1", b""))
                raw = price_entry.get("2")
                if not isinstance(raw, (int, float)):
                    continue
                try:
                    value = _fixed32_to_float(int(raw))
                except (struct.error, TypeError, ValueError):
                    continue
                if component == "Input":
                    input_price = value
                elif component == "Cached input":
                    cached_input_price = value
                elif component == "Output":
                    output_price = value

        # Recommended flag (field 31)
        is_recommended = bool(group.get("31", 0))

        # Context window (field 23.4)
        context_window = config.get("4", 0) or 0

        # Labels (field 30.2 repeated)
        labels = []
        for label_entry in display.get("2", []):
            if isinstance(label_entry, dict):
                label_name = _bytes_to_str(label_entry.get("1", b""))
                label_value_msg = label_entry.get("2", {})
                label_value = ""
                if isinstance(label_value_msg, dict):
                    if "2" in label_value_msg:
                        label_value = _bytes_to_str(label_value_msg["2"])
                    elif "1" in label_value_msg:
                        label_value = str(label_value_msg["1"])
                if label_value:
                    labels.append(f"{label_name}: {label_value}")
                else:
                    labels.append(label_name)

        # Reasoning effort ("Effort: X" / "Reasoning Effort: X" labels),
        # canonicalized so the UI can group variants by effort level.
        reasoning: str | None = None
        for label in labels:
            m = re.match(r"(?:Reasoning\s+)?Effort:\s*(.+)$", label, re.IGNORECASE)
            if m:
                reasoning = _reasoning_key(m.group(1))
                break

        # "Fast Mode: 1" marks the actual ~2x-cost fast variant; a bare
        # "Fast Mode" label only means fast mode is *available*.
        is_fast = "Fast Mode: 1" in labels

        tags: list[str] = []
        if is_free:
            tags.append("Free")
        if "New" in labels:
            tags.append("New")

        # Build label-derived tags.  Context-window information is derived
        # from the context_window field (more reliable than the label, and
        # lets us tag 200K variants that have no "1M Context" label).
        label_tags: list[str] = []
        for label in labels:
            if label == "Fast Mode: 1":
                if "Fast" not in label_tags:
                    label_tags.append("Fast")
            elif label == "Thinking" or label.startswith("Thinking:"):
                if "Thinking" not in label_tags:
                    label_tags.append("Thinking")
            # Effort already appears in display names. Availability-only fast mode,
            # context and prompt-cache-retention labels are not UI tags.

        ctx_tag = _context_tag(context_window)
        if ctx_tag and ctx_tag not in label_tags:
            label_tags.append(ctx_tag)

        # The display name shown to users.  Prefer the group label because it
        # distinguishes variants (e.g. "GLM-5.2 High 1M" vs "GLM-5.2 High").
        display_name = group_label or base_display_name or _pretty_name(internal_id)

        family = model_alias or internal_id

        entry = {
            "display_name": display_name,
            "family": family,
            "family_name": base_display_name or group_label or _pretty_name(internal_id),
            "vision": supports_images,
            "tags": tags,
            "label_tags": label_tags,
            "description": group_label,
            "is_promo": is_free,
            "is_recommended": is_recommended,
            "cost": cost,
            "input_price": input_price,
            "cached_input_price": cached_input_price,
            "output_price": output_price,
            "context_window": context_window,
            "max_output_tokens": config.get("13", 0),
            "labels": labels,
            "reasoning": reasoning,
            "is_fast": is_fast,
            "fusion": None,
        }

        result[internal_id] = entry
        id_map[internal_id] = model_alias or internal_id

    _resolve_fusion_metadata(result)

    # Aliases for known CLI IDs absent from the decoded cache.
    for internal, alias in {
        "MODEL_PRIVATE_11": "claude-haiku-4.5",
        "MODEL_SWE_1_5": "swe-1.5",
        "MODEL_SWE_1_5_SLOW": "swe-1.5",
        "claude-5-fable": "claude-fable-5",
        "gemini-3.0-flash": "gemini-3-flash",
        "deepseek-v4": "deepseek-v4-pro",
    }.items():
        id_map.setdefault(internal, alias)

    return result, id_map


def _resolve_fusion_metadata(result: dict[str, dict]) -> None:
    # Resolve fusion-<lead>-sidekick-<sidekick> after all variants are known.
    # If the lead's -fast ID has no cache entry, retry without the suffix.
    for fusion_id, entry in result.items():
        if not fusion_id.startswith("fusion-"):
            continue
        remainder = fusion_id[len("fusion-"):]
        # ``fusion-sidekick-*`` entries are sidekick component models, not
        # lead+sidekick combos — the separator must appear after the prefix.
        if "-sidekick-" not in remainder:
            continue
        lead_id, sidekick_id = remainder.split("-sidekick-", 1)
        lead_entry = result.get(lead_id) or (
            result.get(lead_id[:-5]) if lead_id.endswith("-fast") else None
        )
        lead_label = next(
            (label[len("Lead: "):] for label in entry.get("labels", []) if label.startswith("Lead: ")),
            None,
        )
        lead_family = (
            lead_entry["family"] if lead_entry
            else (lead_id[:-5] if lead_id.endswith("-fast") else lead_id)
        )
        lead_name = lead_label or (
            lead_entry["family_name"] if lead_entry else _pretty_name(lead_family)
        )
        sk_entry = result.get(sidekick_id)
        sidekick_family = sk_entry["family"] if sk_entry else sidekick_id
        sidekick_name = (
            sk_entry["family_name"] if sk_entry
            else next(
                (label[len("Sidekick: "):] for label in entry.get("labels", []) if label.startswith("Sidekick: ")),
                _pretty_name(sidekick_id),
            )
        )
        sidekick_reasoning = sk_entry["reasoning"] if sk_entry else None
        entry["fusion"] = {
            "lead_family": lead_family,
            "lead_name": lead_name,
            "sidekick_id": sidekick_id,
            "sidekick_family": sidekick_family,
            "sidekick_name": sidekick_name,
            "sidekick_reasoning": sidekick_reasoning,
        }
        if entry["reasoning"] is None and lead_entry:
            entry["reasoning"] = lead_entry["reasoning"]
        # A fusion combo is fast when either side is the fast variant.
        entry["is_fast"] = (
            entry["is_fast"]
            or lead_id.endswith("-fast")
            or bool(sk_entry and sk_entry["is_fast"])
        )


def _parse_cli_model_list(data: Any) -> dict[str, dict]:
    """Read the supported ``devin models list --format json`` catalog.

    Keep only metadata the command actually provides. Cache metadata can
    supply capabilities, structured fusion details and pricing when present;
    a newly listed model must still be usable before those caches catch up.
    Reject an unexpected/partial schema so it cannot replace a good catalog.
    """
    if not isinstance(data, dict) or not isinstance(data.get("families"), list):
        raise ValueError("Model list has no families array")
    models: dict[str, dict] = {}
    for family in data["families"]:
        if not isinstance(family, dict) or not isinstance(family.get("variants"), list):
            raise ValueError("Model family has no variants array")
        for variant in family["variants"]:
            if not isinstance(variant, dict):
                raise ValueError("Model variant is not an object")
            mid = variant.get("model_uid")
            if not isinstance(mid, str) or not mid:
                raise ValueError("Model variant has no model_uid")
            name = variant.get("label") or _pretty_name(mid)
            family_id = family.get("family_uid") or family.get("slug") or mid
            family_name = family.get("family_label") or name
            if not all(isinstance(value, str) for value in (name, family_id, family_name)):
                raise ValueError("Model names must be strings")
            description = variant.get("description")
            entry: dict[str, Any] = {
                "display_name": name,
                "family": family_id,
                "family_name": family_name,
                "description": description if isinstance(description, str) else name,
                "tags": ["New"] if variant.get("is_new") is True else [],
            }
            for src, dst in (("max_context_tokens", "context_window"),
                             ("max_output_tokens", "max_output_tokens")):
                val = variant.get(src)
                if type(val) is int and val >= 0:
                    entry[dst] = val
            # The public catalog exposes effort/fast mode in variant labels.
            effort = re.search(r"\b(No Thinking|None|Minimal|Low|Medium|High|X-?High|Max|Ultra)\b", name, re.I)
            if effort and not mid.startswith("fusion-"):
                entry["reasoning"] = _reasoning_key(effort.group(1))
            summary = variant.get("cost_summary", "")
            if isinstance(summary, str):
                price_fields = {"Input": "input_price", "Cached input": "cached_input_price",
                                "Output": "output_price"}
                for amount, component in re.findall(r"\$(\d+(?:\.\d+)?) / 1M (Cached input|Input|Output)\b", summary):
                    entry[price_fields[component]] = float(amount)
                if summary.strip().lower() == "free":
                    entry.update({key: 0.0 for key in price_fields.values()})
                    entry["tags"].append("Free")
            models[mid] = entry
    if not models:
        raise ValueError("Model list is empty")
    for mid, entry in models.items():
        # A family named "Fast" is not necessarily a priority variant.
        base_id = re.sub(r"-(fast|lightning)$", "", mid)
        base = models.get(base_id) if base_id != mid else None
        if mid.endswith("-priority") or (base and base["family"] == entry["family"]):
            entry["is_fast"] = True
    return models
