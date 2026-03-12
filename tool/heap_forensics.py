# -*- coding: utf-8 -*-
"""
Heap Snapshot parser: WeakMap -> table, only nodes with id/parentId/children/message.
Generates structure_report.html and conversation_threads.html.
"""

import json
import os
import re
import sys
from datetime import datetime

NODE_FIELD_COUNT = 6
EDGE_FIELD_COUNT = 3
UUID_PATTERN = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def contains_uuid(s: str) -> bool:
    return bool(UUID_PATTERN.search(s)) if s else False


def load_snapshot(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_node_meta(snapshot: dict) -> dict:
    meta = snapshot["snapshot"]["meta"]
    return {
        "node_fields": meta["node_fields"],
        "node_types": meta["node_types"],
        "edge_fields": meta["edge_fields"],
        "edge_types": meta["edge_types"],
    }


def get_node(snapshot: dict, node_index: int) -> dict:
    nodes = snapshot["nodes"]
    strings = snapshot["strings"]
    meta = get_node_meta(snapshot)
    type_names = meta["node_types"][0]
    base = node_index * NODE_FIELD_COUNT
    type_id = nodes[base + 0]
    name_id = nodes[base + 1]
    id_val = nodes[base + 2]
    self_size = nodes[base + 3]
    edge_count = nodes[base + 4]
    detachedness = nodes[base + 5]
    name_str = strings[name_id] if name_id < len(strings) else f"<string#{name_id}>"
    type_str = type_names[type_id] if type_id < len(type_names) else f"type#{type_id}"
    return {
        "node_index": node_index,
        "type": type_str,
        "type_id": type_id,
        "name": name_str,
        "name_id": name_id,
        "id": id_val,
        "self_size": self_size,
        "edge_count": edge_count,
        "detachedness": detachedness,
    }


def get_edge_offsets(snapshot: dict) -> list[int]:
    nodes = snapshot["nodes"]
    node_count = snapshot["snapshot"]["node_count"]
    out = [0] * node_count
    offset = 0
    for i in range(node_count):
        out[i] = offset
        offset += nodes[i * NODE_FIELD_COUNT + 4] * EDGE_FIELD_COUNT
    return out


def get_edges_from_node(snapshot: dict, node_index: int, edge_offsets: list[int] | None = None) -> list:
    nodes = snapshot["nodes"]
    edges = snapshot["edges"]
    meta = get_node_meta(snapshot)
    edge_type_names = meta["edge_types"][0]
    strings = snapshot["strings"]
    if edge_offsets is not None:
        offset = edge_offsets[node_index]
    else:
        offset = 0
        for i in range(node_index):
            offset += nodes[i * NODE_FIELD_COUNT + 4] * EDGE_FIELD_COUNT
    base = node_index * NODE_FIELD_COUNT
    edge_count = nodes[base + 4]
    result = []
    for i in range(edge_count):
        t = edges[offset + i * EDGE_FIELD_COUNT + 0]
        name_or_idx = edges[offset + i * EDGE_FIELD_COUNT + 1]
        to_node = edges[offset + i * EDGE_FIELD_COUNT + 2]
        type_str = edge_type_names[t] if t < len(edge_type_names) else f"edge_type#{t}"
        label = strings[name_or_idx] if (type_str == "property" or type_str == "internal") and name_or_idx < len(strings) else str(name_or_idx)
        to_idx = to_node // NODE_FIELD_COUNT if to_node % NODE_FIELD_COUNT == 0 else to_node
        result.append({
            "type": type_str,
            "name_or_index": name_or_idx,
            "label": label,
            "to_node": to_node,
            "to_node_index": to_idx,
        })
    return result


def to_node_index(edge_to_node_value: int) -> int:
    if edge_to_node_value % NODE_FIELD_COUNT == 0:
        return edge_to_node_value // NODE_FIELD_COUNT
    return edge_to_node_value


def build_depth1_tree(snapshot: dict, root_index: int = 0, edge_offsets: list[int] | None = None) -> dict:
    root = get_node(snapshot, root_index)
    out_edges = get_edges_from_node(snapshot, root_index, edge_offsets)
    children = []
    for e in out_edges:
        child_node = get_node(snapshot, e["to_node_index"])
        child_node["edge_from_parent"] = e
        children.append(child_node)
    root["children"] = children
    return root


def build_depth_n_tree(
    snapshot: dict,
    root_index: int,
    max_depth: int,
    edge_offsets: list[int] | None = None,
    edge_from_parent: dict | None = None,
) -> dict:
    root = get_node(snapshot, root_index)
    if max_depth <= 0:
        root["children"] = []
        return root
    out_edges = get_edges_from_node(snapshot, root_index, edge_offsets)
    children = []
    parent_label = str(edge_from_parent.get("label", "") if edge_from_parent else "").strip().lower()
    for e in out_edges:
        next_depth = max_depth - 1
        child_label = str(e.get("label", "")).strip().lower()
        if parent_label == "message" and child_label == "metadata":
            next_depth += 2
        child = build_depth_n_tree(snapshot, e["to_node_index"], next_depth, edge_offsets, edge_from_parent=e)
        child["edge_from_parent"] = e
        children.append(child)
    root["children"] = children
    return root


def find_all_nodes_by_exact_name(snapshot: dict, exact_name: str) -> list[int]:
    nodes = snapshot["nodes"]
    strings = snapshot["strings"]
    node_count = snapshot["snapshot"]["node_count"]
    result = []
    for node_index in range(node_count):
        name_id = nodes[node_index * NODE_FIELD_COUNT + 1]
        if name_id < len(strings) and strings[name_id] == exact_name:
            result.append(node_index)
    return result


def find_child_by_exact_name_and_edge(snapshot: dict, parent_index: int, exact_name: str, edge_offsets: list[int] | None = None) -> tuple[int | None, dict | None]:
    edges = get_edges_from_node(snapshot, parent_index, edge_offsets)
    strings = snapshot["strings"]
    exact_lower = exact_name.lower()
    for e in edges:
        name_or_idx = e.get("name_or_index", -1)
        if isinstance(name_or_idx, int) and 0 <= name_or_idx < len(strings):
            label = str(strings[name_or_idx]).strip()
        else:
            label = str(e.get("label", "")).strip()
        if label.lower() == exact_lower:
            return e["to_node_index"], e
        child = get_node(snapshot, e["to_node_index"])
        if (child.get("name") or "").strip().lower() == exact_lower:
            return e["to_node_index"], e
    return None, None


def get_object_with_required_props(snapshot: dict, node_index: int, edge_offsets: list[int] | None) -> int:
    """Return node_index if it has id/parentId/children/message; else return a direct child (object) that has them. Return -1 if none."""
    if _node_has_required_props(snapshot, node_index, edge_offsets):
        return node_index
    for e in get_edges_from_node(snapshot, node_index, edge_offsets):
        ci = e["to_node_index"]
        if get_node(snapshot, ci).get("type") == "object" and _node_has_required_props(snapshot, ci, edge_offsets):
            return ci
    return -1


REQUIRED_PROP_NAMES = ("id", "parentId", "children", "message")


def _node_has_required_props(snapshot: dict, node_index: int, edge_offsets: list[int] | None) -> bool:
    node = get_node(snapshot, node_index)
    if node.get("type") != "object":
        return False
    edges = get_edges_from_node(snapshot, node_index, edge_offsets)
    strings = snapshot["strings"]
    seen_lower = set()
    for e in edges:
        name_or_idx = e.get("name_or_index", -1)
        if isinstance(name_or_idx, int) and 0 <= name_or_idx < len(strings):
            label = strings[name_or_idx]
            if isinstance(label, str) and label.strip():
                seen_lower.add(label.strip().lower())
        label = str(e.get("label", "")).strip()
        if label and not label.startswith("[") and not label.isdigit():
            seen_lower.add(label.lower())
    return all(name.lower() in seen_lower for name in REQUIRED_PROP_NAMES)


def object_has_id_parentid_children_message_structure(snapshot: dict, node_index: int, edge_offsets: list[int] | None = None) -> bool:
    if _node_has_required_props(snapshot, node_index, edge_offsets):
        return True
    edges = get_edges_from_node(snapshot, node_index, edge_offsets)
    for e in edges:
        child_idx = e["to_node_index"]
        if get_node(snapshot, child_idx).get("type") == "object" and _node_has_required_props(snapshot, child_idx, edge_offsets):
            return True
    return False


def get_property_node(snapshot: dict, obj_index: int, prop_name: str, edge_offsets: list[int] | None) -> int | None:
    """Return the child node index linked by the prop_name edge from the object node. None if not found."""
    idx, _ = find_child_by_exact_name_and_edge(snapshot, obj_index, prop_name, edge_offsets)
    return idx


def get_property_string(snapshot: dict, obj_index: int, prop_name: str, edge_offsets: list[int] | None) -> str | None:
    """Return the string value of prop_name on the object (when the child is a string-like node)."""
    idx = get_property_node(snapshot, obj_index, prop_name, edge_offsets)
    if idx is None:
        return None
    node = get_node(snapshot, idx)
    s = _node_name_as_string(snapshot, node)
    if s:
        return s
    return (str(node.get("name", "")).strip() or None) if node.get("name") else None


def get_property_number(snapshot: dict, obj_index: int, prop_name: str, edge_offsets: list[int] | None) -> int | float | None:
    """Return the numeric value of prop_name on the object."""
    idx = get_property_node(snapshot, obj_index, prop_name, edge_offsets)
    if idx is None:
        return None
    node = get_node(snapshot, idx)
    name = node.get("name", "")
    try:
        if "." in str(name):
            return float(name)
        return int(name)
    except (ValueError, TypeError):
        return None


def get_children_ids_from_message(snapshot: dict, message_node_index: int, edge_offsets: list[int] | None) -> list[str]:
    """Return list of id strings from each element (object) of the message's children array."""
    children_idx = get_property_node(snapshot, message_node_index, "children", edge_offsets)
    if children_idx is None:
        return []
    node = get_node(snapshot, children_idx)
    if node.get("type") != "array" and node.get("type") != "object":
        return []
    ids = []
    for e in get_edges_from_node(snapshot, children_idx, edge_offsets):
        child_idx = e["to_node_index"]
        child_node = get_node(snapshot, child_idx)
        if child_node.get("type") != "object":
            continue
        sid = get_property_string(snapshot, child_idx, "id", edge_offsets)
        if sid:
            ids.append(sid)
    return ids


def get_author_role(snapshot: dict, message_node_index: int, edge_offsets: list[int] | None) -> str:
    """Return author.role from the message object (user / assistant / tool)."""
    author_idx = get_property_node(snapshot, message_node_index, "author", edge_offsets)
    if author_idx is None:
        return "unknown"
    role = get_property_string(snapshot, author_idx, "role", edge_offsets)
    return (role or "unknown").lower()


def get_message_metadata_title(snapshot: dict, message_node_index: int, edge_offsets: list[int] | None) -> str | None:
    """Return metadata.async_task_title or metadata.image_gen_title (from message node)."""
    meta_idx = get_property_node(snapshot, message_node_index, "metadata", edge_offsets)
    if meta_idx is None:
        return None
    for key in ("async_task_title", "image_gen_title"):
        title = get_property_string(snapshot, meta_idx, key, edge_offsets)
        if title and title.strip():
            return title.strip()
    return None


def _node_name_as_string(snapshot: dict, node: dict) -> str | None:
    """Return node's name as string value if it looks like content (string/hidden/concatenated/sliced)."""
    t = (node.get("type") or "").lower()
    name = node.get("name")
    if name is None:
        return None
    s = str(name).strip() if isinstance(name, str) else str(name)
    if not s:
        return None
    if t in ("string", "hidden", "concatenated string", "sliced string", "synthetic"):
        return s
    if t == "object" and len(s) > 2 and s[0] != "<":
        return s
    return None


_SKIP_NAMES = frozenset({"array", "object", "string", "hidden", "number", "boolean", "undefined", "null"})


def _first_string_from_object(snapshot: dict, obj_index: int, edge_offsets: list[int] | None) -> str | None:
    """Get first string property value from an object (for array elements that are wrappers)."""
    for key in ("value", "0", "content", "text", "data"):
        s = get_property_string(snapshot, obj_index, key, edge_offsets)
        if s and s.strip() and s.strip().lower() not in _SKIP_NAMES:
            return s.strip()
    for e in get_edges_from_node(snapshot, obj_index, edge_offsets):
        node = get_node(snapshot, e["to_node_index"])
        s = _node_name_as_string(snapshot, node)
        if s and s.strip() and s.strip().lower() not in _SKIP_NAMES and len(s) > 2:
            return s.strip()
    return None


def _collect_array_strings(snapshot: dict, array_node_index: int, edge_offsets: list[int] | None) -> list[str]:
    """Collect string values from an array node. Handles direct strings and object wrappers.
    Skips type names (Array, Object)."""
    edges = get_edges_from_node(snapshot, array_node_index, edge_offsets)
    indexed = []
    for e in edges:
        label = str(e.get("label", ""))
        try:
            i = int(label)
        except ValueError:
            continue
        child_idx = e["to_node_index"]
        node = get_node(snapshot, child_idx)
        s = _node_name_as_string(snapshot, node)
        if s is None and node.get("type") in ("object", "array"):
            s = _first_string_from_object(snapshot, child_idx, edge_offsets)
        if s and s.strip() and s.strip().lower() not in _SKIP_NAMES:
            indexed.append((i, s.strip()))
    indexed.sort(key=lambda x: x[0])
    return [s for _, s in indexed]


def get_all_text_from_message_parts(
    snapshot: dict, msg_node_index: int, edge_offsets: list[int] | None
) -> list[str]:
    """
    Collect full message text from message.content.parts[*].text[*].
    - Follows: message -> content -> parts (array)
    - For each part in parts[0], parts[1], ...:
        - If part.text exists and is array/object: take text[0], text[1], ... in index order
        - Else, fall back to the part object itself (first string-like value or name)
    This avoids only looking at parts[0] and ensures very long messages spanning multiple parts are fully captured.
    """
    result: list[str] = []

    # message.content
    content_idx, _ = find_child_by_exact_name_and_edge(snapshot, msg_node_index, "content", edge_offsets)
    if content_idx is None:
        return result

    # content.parts
    parts_idx, _ = find_child_by_exact_name_and_edge(snapshot, content_idx, "parts", edge_offsets)
    if parts_idx is None:
        return result

    # parts 배열의 각 요소(0,1,2,...)를 인덱스 순서로 순회
    edges = get_edges_from_node(snapshot, parts_idx, edge_offsets)
    indexed_parts: list[tuple[int, int]] = []
    for e in edges:
        label = str(e.get("label", ""))
        try:
            i = int(label)
        except ValueError:
            continue
        indexed_parts.append((i, e["to_node_index"]))

    indexed_parts.sort(key=lambda x: x[0])

    for _idx, part_idx in indexed_parts:
        # 1) part.text 배열이 직접 있는 경우
        text_node = get_property_node(snapshot, part_idx, "text", edge_offsets)
        if text_node is not None:
            node = get_node(snapshot, text_node)
            if node.get("type") in ("array", "object"):
                part_strings = _collect_array_strings(snapshot, text_node, edge_offsets)
                result.extend(part_strings)
            else:
                s = _node_name_as_string(snapshot, node)
                if s and s.strip() and s.strip().lower() not in _SKIP_NAMES:
                    result.append(s.strip())
            continue

        # 2) part.elements[*].text[*] 구조를 사용하는 경우 (폴백)
        elements_idx = get_property_node(snapshot, part_idx, "elements", edge_offsets)
        if elements_idx is not None:
            part_strings = _collect_array_strings(snapshot, elements_idx, edge_offsets)
            result.extend(part_strings)
            continue

        # 3) 그래도 없으면 part 객체에서 첫 문자열 / 이름을 사용 (최후의 폴백)
        s = _first_string_from_object(snapshot, part_idx, edge_offsets)
        if s:
            result.append(s)
        else:
            node = get_node(snapshot, part_idx)
            s = _node_name_as_string(snapshot, node)
            if s and s.strip() and s.strip().lower() not in _SKIP_NAMES:
                result.append(s.strip())

    return result


def get_author_display_info(snapshot: dict, message_node_index: int, edge_offsets: list[int] | None) -> dict:
    """Return author.name, .role, and author.metadata.real_author, .source for tool display."""
    author_idx = get_property_node(snapshot, message_node_index, "author", edge_offsets)
    if author_idx is None:
        return {"name": None, "role": None, "real_author": None, "source": None}
    name = get_property_string(snapshot, author_idx, "name", edge_offsets)
    role = get_property_string(snapshot, author_idx, "role", edge_offsets)
    author_meta_idx = get_property_node(snapshot, author_idx, "metadata", edge_offsets)
    real_author = get_property_string(snapshot, author_meta_idx, "real_author", edge_offsets) if author_meta_idx is not None else None
    source = get_property_string(snapshot, author_meta_idx, "source", edge_offsets) if author_meta_idx is not None else None
    return {"name": name or None, "role": role or None, "real_author": real_author or None, "source": source or None}


def _get_property_string_or_name(snapshot: dict, obj_index: int, prop_name: str, edge_offsets: list[int] | None) -> str | None:
    """Get property as string; if child is object/other type, return its name if it looks like string content."""
    idx = get_property_node(snapshot, obj_index, prop_name, edge_offsets)
    if idx is None:
        return None
    node = get_node(snapshot, idx)
    s = (node.get("name") or "")
    if isinstance(s, str) and s.strip():
        return s.strip()
    if isinstance(s, (int, float)) and node.get("type") == "number":
        return str(s)
    return None


def _get_object_string_props(snapshot: dict, obj_index: int, edge_offsets: list[int] | None) -> dict[str, str]:
    """Return dict of property_name -> string value for all direct string (or string-like) children."""
    out = {}
    for e in get_edges_from_node(snapshot, obj_index, edge_offsets):
        if e.get("type") not in ("property", "internal"):
            continue
        label = str(e.get("label", "")).strip()
        if not label or label.isdigit():
            continue
        child_idx = e["to_node_index"]
        node = get_node(snapshot, child_idx)
        s = _node_name_as_string(snapshot, node)
        if s is None and node.get("type") in ("object", "array"):
            s = _first_string_from_object(snapshot, child_idx, edge_offsets)
        if s is None:
            s = (node.get("name") or "")
            s = str(s).strip() if s else ""
        if s and s.strip().lower() not in _SKIP_NAMES:
            out[label] = s.strip()
    return out


def get_tool_metadata_summary(
    snapshot: dict, message_node_index: int, object_node_index: int | None, edge_offsets: list[int] | None
) -> dict:
    """Extract tool message metadata: search_model_queries, search_result_groups, image_gen_title, async_task_title.
    Tries message.metadata first; if empty, tries object (parent)."""
    result = {"search_queries": [], "search_result_groups": [], "image_title": None, "status": None, "message_type": None}
    result["status"] = get_property_string(snapshot, message_node_index, "status", edge_offsets)
    meta_idx = get_property_node(snapshot, message_node_index, "metadata", edge_offsets)
    if meta_idx is not None:
        result["message_type"] = get_property_string(snapshot, meta_idx, "message_type", edge_offsets)
        for key in ("async_task_title", "image_gen_title"):
            t = get_property_string(snapshot, meta_idx, key, edge_offsets)
            if t and t.strip():
                result["image_title"] = t.strip()
                break
    if result["image_title"] is None and object_node_index is not None:
        result["image_title"] = get_metadata_title_from_object(snapshot, object_node_index, edge_offsets)
    if meta_idx is None and object_node_index is not None:
        meta_idx = get_property_node(snapshot, object_node_index, "metadata", edge_offsets)
    if meta_idx is None:
        for e in get_edges_from_node(snapshot, message_node_index, edge_offsets):
            cidx = e["to_node_index"]
            if get_property_node(snapshot, cidx, "search_model_queries", edge_offsets) is not None or get_property_node(snapshot, cidx, "search_result_groups", edge_offsets) is not None:
                meta_idx = cidx
                break
    if meta_idx is None and object_node_index is not None:
        for e in get_edges_from_node(snapshot, object_node_index, edge_offsets):
            cidx = e["to_node_index"]
            if get_property_node(snapshot, cidx, "search_model_queries", edge_offsets) is not None or get_property_node(snapshot, cidx, "search_result_groups", edge_offsets) is not None:
                meta_idx = cidx
                break
    if meta_idx is None:
        return result
    # metadata.search_model_queries.queries -> array of strings (V8 may store as .queries.elements)
    smq_idx = get_property_node(snapshot, meta_idx, "search_model_queries", edge_offsets)
    if smq_idx is not None:
        queries_idx = get_property_node(snapshot, smq_idx, "queries", edge_offsets)
        if queries_idx is not None:
            array_idx = get_property_node(snapshot, queries_idx, "elements", edge_offsets)
            if array_idx is None:
                array_idx = queries_idx
            result["search_queries"] = _collect_array_strings(snapshot, array_idx, edge_offsets)
    # metadata.search_result_groups -> array of { domain, entries } (V8 may store as .elements)
    srg_idx = get_property_node(snapshot, meta_idx, "search_result_groups", edge_offsets)
    if srg_idx is not None:
        groups_array = get_property_node(snapshot, srg_idx, "elements", edge_offsets)
        if groups_array is None:
            groups_array = srg_idx
        for e in get_edges_from_node(snapshot, groups_array, edge_offsets):
            group_idx = e["to_node_index"]
            group_node = get_node(snapshot, group_idx)
            if group_node.get("type") not in ("object", "array"):
                continue
            props = _get_object_string_props(snapshot, group_idx, edge_offsets)
            domain = props.get("domain") or _get_property_string_or_name(snapshot, group_idx, "domain", edge_offsets) or ""
            entries_idx = get_property_node(snapshot, group_idx, "entries", edge_offsets)
            entries = []
            if entries_idx is not None:
                entries_array = get_property_node(snapshot, entries_idx, "elements", edge_offsets)
                if entries_array is None:
                    entries_array = entries_idx
                for e2 in get_edges_from_node(snapshot, entries_array, edge_offsets):
                    ent_idx = e2["to_node_index"]
                    ent_props = _get_object_string_props(snapshot, ent_idx, edge_offsets)
                    title = ent_props.get("title") or _get_property_string_or_name(snapshot, ent_idx, "title", edge_offsets) or ""
                    url = ent_props.get("url") or _get_property_string_or_name(snapshot, ent_idx, "url", edge_offsets) or ""
                    snippet = ent_props.get("snippet") or _get_property_string_or_name(snapshot, ent_idx, "snippet", edge_offsets) or ""
                    entries.append({"title": title, "url": url, "snippet": snippet.strip()})
            result["search_result_groups"].append({"domain": domain, "entries": entries})
    return result


def format_tool_content_and_label(
    author_info: dict, tool_meta: dict
) -> tuple[str, str]:
    """Build display content and role label for a tool message from author + metadata.
    Returns (content_text, role_label_suffix) e.g. ('Search: ...', 'web.run') or ('Image: ...', 'Image')."""
    name = (author_info.get("name") or "").strip()
    real_author = (author_info.get("real_author") or "").strip()
    source = (author_info.get("source") or "").strip()
    if "tool:web" in real_author or "web" in (name or "").lower():
        label_suffix = name or "web"
    elif tool_meta.get("image_title"):
        label_suffix = "Image"
    else:
        label_suffix = name or "tool"
    parts = []
    if tool_meta.get("search_queries"):
        parts.append("Search queries: " + " | ".join(f'"{q}"' for q in tool_meta["search_queries"] if q))
    if tool_meta.get("search_result_groups"):
        for g in tool_meta["search_result_groups"][:15]:
            domain = g.get("domain") or ""
            entries = g.get("entries") or []
            if domain:
                parts.append(f"\n• {domain}")
            for ent in entries[:5]:
                title = (ent.get("title") or "").strip()
                url = (ent.get("url") or "").strip()
                if title or url:
                    parts.append(f"  - {title}" + (f" ({url})" if url else ""))
    if tool_meta.get("image_title"):
        parts.append("Image: " + tool_meta["image_title"])
    if not parts:
        parts.append("(Tool output)")
    content = "\n".join(p for p in parts if p).strip()
    return content, label_suffix


def get_metadata_title_from_object(snapshot: dict, object_node_index: int, edge_offsets: list[int] | None) -> str | None:
    """Return metadata.async_task_title or image_gen_title from root object (sibling of message)."""
    meta_idx = get_property_node(snapshot, object_node_index, "metadata", edge_offsets)
    if meta_idx is None:
        return None
    for key in ("async_task_title", "image_gen_title"):
        title = get_property_string(snapshot, meta_idx, key, edge_offsets)
        if title and title.strip():
            return title.strip()
    return None


def get_create_time_value(snapshot: dict, message_node_index: int, edge_offsets: list[int] | None) -> str | None:
    """Return only the value under message.create_time (e.g. timestamp)."""
    ct_idx = get_property_node(snapshot, message_node_index, "create_time", edge_offsets)
    if ct_idx is None:
        return None
    val_idx = get_property_node(snapshot, ct_idx, "value", edge_offsets)
    if val_idx is None:
        return None
    n = get_node(snapshot, val_idx)
    raw = n.get("name")
    if raw is None:
        return None
    s = (str(raw).strip() if isinstance(raw, str) else str(raw))
    return s if s else None


def collect_strings_from_elements_tree(tree_node: dict) -> list[str]:
    """Recursively collect name (string content) of nodes with type string from the elements tree."""
    out = []
    if tree_node.get("type") == "string":
        name = (tree_node.get("name") or "").strip()
        if name:
            out.append(name)
    for ch in tree_node.get("children", []):
        out.extend(collect_strings_from_elements_tree(ch))
    return out


def _is_asset_or_metadata_string(s: str) -> bool:
    """True if string looks like image/asset pointer or heap metadata, not user-facing text."""
    if not s or len(s) < 20:
        return False
    s_lower = s.lower()
    if "image_asset_pointer" in s_lower or "sediment://" in s_lower or "asset_pointer" in s_lower:
        return True
    if "content_type" in s_lower and ("size_bytes" in s_lower or "asset_pointer" in s_lower):
        return True
    if s.count("null") >= 3 and ("object" in s_lower or "boolean" in s_lower) and ("content_type" in s_lower or "size_bytes" in s_lower or "fovea" in s_lower):
        return True
    if len(s) > 200 and s.count(" null ") >= 2 and "object" in s_lower:
        return True
    return False


def _extract_trailing_text_from_mixed_string(s: str) -> str | None:
    """If string contains asset blob + user text (e.g. '...metadata do you know what it is?'), return the trailing text."""
    if not s or not s.strip():
        return None
    # Try split after 'metadata' (with or without trailing space)
    for sep in ("metadata ", "metadata"):
        if sep in s:
            tail = s.split(sep)[-1].strip()
            # No length limit: keep full tail if it looks like human text (not an asset URL)
            if tail and any(c.isalpha() for c in tail) and "sediment://" not in tail and "content_type" not in tail:
                return tail
    # Or after last ' null object ' (heap structure)
    if " null object " in s:
        segments = s.split(" null object ")
        for seg in reversed(segments):
            seg = seg.strip()
            # No length limit here either; only basic heuristics to avoid pure blob
            if seg and any(c.isalpha() for c in seg) and "sediment://" not in seg and "asset_pointer" not in seg:
                return seg
    # Fallback: known user question after blob (e.g. "... metadata do you know what it is?")
    if " do you know what it is?" in s:
        return "do you know what it is?"
    if " do " in s and "?" in s:
        idx = s.rfind(" do ")
        if idx != -1:
            tail = s[idx + 4 :].strip()
            # Keep full tail sentence, regardless of length
            if tail and "object" not in tail and "null" not in tail:
                return tail
    # Trailing segment that looks like a sentence (ends with ?)
    if s.strip().endswith("?"):
        words = s.split()
        for i in range(len(words), 0, -1):
            tail = " ".join(words[-i:])
            # Allow long questions: do not cut at 100 chars
            if tail.endswith("?") and "object" not in tail and "null" not in tail and "asset_pointer" not in tail:
                return tail
    return None


def _strip_remaining_blob(content: str) -> str:
    """If content still contains asset blob text, remove it and keep only the human message (e.g. 'do you know what it is?')."""
    if not content or len(content) < 50:
        return content
    if "image_asset_pointer" not in content and "content_type" not in content:
        return content
    # Blob present: keep only the trailing human part
    extracted = _extract_trailing_text_from_mixed_string(content)
    if extracted:
        return extracted
    if " do you know what it is?" in content:
        return "do you know what it is?"
    if " metadata " in content:
        tail = content.split(" metadata ")[-1].strip()
        # Also here, no artificial max length on tail
        if tail and "object" not in tail and "null" not in tail:
            return tail
    return content


def _parse_content_parts(strings: list[str], role: str) -> tuple[str, str]:
    """Split into text (user message) and media summary. Dedupe consecutive duplicates.
    Returns (content_display, media_summary). Never include asset blob in content."""
    deduped = []
    for s in strings:
        if not s.strip():
            continue
        if deduped and deduped[-1] == s:
            continue
        deduped.append(s)
    text_parts = []
    has_media = False
    for s in deduped:
        if _is_asset_or_metadata_string(s):
            has_media = True
            trailing = _extract_trailing_text_from_mixed_string(s)
            if trailing:
                text_parts.append(trailing)
            continue
        # Don't add strings that look like blob even if not fully matched
        if len(s) > 100 and " null " in s and " object " in s and "content_type" in s:
            has_media = True
            trailing = _extract_trailing_text_from_mixed_string(s)
            if trailing:
                text_parts.append(trailing)
            continue
        text_parts.append(s)
    content = " ".join(text_parts).strip()
    content = _strip_remaining_blob(content)
    media_summary = ""
    if has_media and role != "tool":
        media_summary = " [Image attached]" if content else "[Image]"
    elif has_media and role == "tool":
        media_summary = "[Image]"
    return content, media_summary


def get_message_content_parts_elements_tree(
    snapshot: dict, object_node_index: int, edge_offsets: list[int] | None, elements_depth: int = 4
) -> tuple[list[tuple[dict, dict]], dict] | None:
    """Follow object -> message -> content -> parts -> elements and return the elements node and its subtree.
    Returns ([(message_node, message_edge), ...], elements_tree) or None. Matches [internal] elements too."""
    if object_node_index < 0:
        return None
    msg_idx, msg_edge = find_child_by_exact_name_and_edge(snapshot, object_node_index, "message", edge_offsets)
    if msg_idx is None:
        return None
    content_idx, content_edge = find_child_by_exact_name_and_edge(snapshot, msg_idx, "content", edge_offsets)
    if content_idx is None:
        return None
    parts_idx, parts_edge = find_child_by_exact_name_and_edge(snapshot, content_idx, "parts", edge_offsets)
    if parts_idx is None:
        return None
    elements_idx, elements_edge = find_child_by_exact_name_and_edge(snapshot, parts_idx, "elements", edge_offsets)
    if elements_idx is None:
        return None
    message_node = get_node(snapshot, msg_idx)
    message_node["edge_from_parent"] = msg_edge
    content_node = get_node(snapshot, content_idx)
    content_node["edge_from_parent"] = content_edge
    parts_node = get_node(snapshot, parts_idx)
    parts_node["edge_from_parent"] = parts_edge
    elements_node = get_node(snapshot, elements_idx)
    elements_node["edge_from_parent"] = elements_edge
    elements_tree = build_depth_n_tree(snapshot, elements_idx, elements_depth, edge_offsets)
    path_chain = [
        (message_node, msg_edge),
        (content_node, content_edge),
        (parts_node, parts_edge),
        (elements_node, elements_edge),
    ]
    return (path_chain, elements_tree)


def type_badge(t: str) -> str:
    colors = {"object": "#4CAF50", "string": "#2196F3", "array": "#FF9800", "closure": "#9C27B0", "code": "#795548", "synthetic": "#607D8B", "native": "#E91E63", "hidden": "#455A64"}
    return f'<span class="badge" style="background:{colors.get(t, "#757575")}">{t}</span>'


def escape_html(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


# Match entity["type","Display Name","description"] (CDP/heap format). Captures: (type, name, desc).
# Allow optional ** before/after (so **entity[...]** is handled before bold regex)
# PUA \ue200\ue201\ue202 often wrap "entity[" in heap
_ZW = r"[\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\s\u2000-\u200b\ue200-\ue202]*"
_ENTITY_WORD = _ZW.join(list("entity"))
_Q = r'(?:\\")?'
_OPT_BOLD = r"\*\*?"  # optional ** at start
_ENTITY_FULL_PAT = (
    _OPT_BOLD + _ZW + _ENTITY_WORD + _ZW + r"\[\s*"
    + _Q + r"([^\"]*)" + _Q + r"\s*,\s*"
    + _Q + r"([^\"]*)" + _Q + r"\s*,\s*"
    + _Q + r"([^\"]*)" + _Q + r"\s*\]" + _ZW + _OPT_BOLD
)
ENTITY_FULL_RE = re.compile(_ENTITY_FULL_PAT)
_ENTITY_2_PAT = (
    _OPT_BOLD + _ZW + _ENTITY_WORD + _ZW + r"\[\s*"
    + _Q + r"[^\"]*" + _Q + r"\s*,\s*"
    + _Q + r"([^\"]*)" + _Q + r"\s*\]" + _ZW + _OPT_BOLD
)
ENTITY_2_RE = re.compile(_ENTITY_2_PAT)
# Fallback: ** (any chars) entity ["a","b","c"] ** — .*? allows any invisible/weird chars
_ENTITY_SIMPLE_FULL = re.compile(
    r"\*\*.*?entity\s*\[\s*\"([^\"]*)\"\s*,\s*\"([^\"]*)\"\s*,\s*\"([^\"]*)\"\s*\]\s*\*\*",
    re.DOTALL,
)
_ENTITY_SIMPLE_2 = re.compile(
    r"\*\*.*?entity\s*\[\s*\"[^\"]*\"\s*,\s*\"([^\"]*)\"\s*\]\s*\*\*",
    re.DOTALL,
)
# No **: entity["type","name","desc"] or entity["type","name"] only
_ENTITY_NO_BOLD_FULL = re.compile(
    r'entity\s*\[\s*"([^"]*)"\s*,\s*"([^"]*)"\s*,\s*"([^"]*)"\s*\]'
)
_ENTITY_NO_BOLD_2 = re.compile(
    r'entity\s*\[\s*"[^"]*"\s*,\s*"([^"]*)"\s*\]'
)


def _pua_to_unicode_emoji(s: str) -> str:
    """Map Apple/Japanese PUA (e.g. U+F604) to standard Unicode emoji (U+1F604) so they render in browsers."""
    out = []
    for c in s:
        o = ord(c)
        if 0xF300 <= o <= 0xF64F:
            out.append(chr(0x1F300 + (o - 0xF300)))
        else:
            out.append(c)
    return "".join(out)


def _fix_unicode_for_display(s: str) -> str:
    """Fix surrogate pairs (e.g. from JSON \\uD83D\\uDE0A) into single emoji; replace lone surrogates with �.
    Map Apple PUA emoji (F300–F64F) to Unicode emoji (1F300–1F64F). Keeps valid UTF-8 displayable."""
    if not s:
        return s
    # Replace lone surrogates so we can safely run surrogatepair → character
    s = re.sub(r"[\uD800-\uDBFF](?![\uDC00-\uDFFF])", "\uFFFD", s)
    s = re.sub(r"(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]", "\uFFFD", s)
    s = s.encode("utf-16", "surrogatepass").decode("utf-16")
    return _pua_to_unicode_emoji(s)


def _build_content_with_entity_spans(out: str) -> str:
    """Replace entity["type","name","desc"] with name (type; desc) — name is message, type/desc are explanation in parentheses. Strip surrounding **."""
    matches = []
    for m in ENTITY_FULL_RE.finditer(out):
        matches.append((m.start(), m.end(), "3", (m.group(1), m.group(2), m.group(3))))
    for m in ENTITY_2_RE.finditer(out):
        if any(start <= m.start() < end for start, end, _, _ in matches):
            continue
        matches.append((m.start(), m.end(), "2", (m.group(1),)))
    if not matches:
        for m in _ENTITY_SIMPLE_FULL.finditer(out):
            matches.append((m.start(), m.end(), "3", (m.group(1), m.group(2), m.group(3))))
    if not matches:
        for m in _ENTITY_SIMPLE_2.finditer(out):
            if not any(start <= m.start() < end for start, end, _, _ in matches):
                matches.append((m.start(), m.end(), "2", (m.group(1),)))
    if not matches:
        for m in _ENTITY_NO_BOLD_FULL.finditer(out):
            matches.append((m.start(), m.end(), "3", (m.group(1), m.group(2), m.group(3))))
    if not matches:
        for m in _ENTITY_NO_BOLD_2.finditer(out):
            if not any(start <= m.start() < end for start, end, _, _ in matches):
                matches.append((m.start(), m.end(), "2", (m.group(1),)))
    matches.sort(key=lambda x: x[0])
    if not matches:
        return escape_html(out)
    parts = []
    last_end = 0
    for start, end, kind, grps in matches:
        parts.append(escape_html(out[last_end:start]))
        if kind == "3":
            etype, name, desc = grps
            desc_part = [p.strip() for p in (etype, desc) if p and p.strip()]
            desc_str = "; ".join(desc_part) if desc_part else ""
            if desc_str:
                parts.append(f'<span class="entity-ref">{escape_html(name)} <span class="entity-desc">({escape_html(desc_str)})</span></span>')
            else:
                parts.append(f'<span class="entity-ref">{escape_html(name)}</span>')
        else:
            name = grps[0]
            parts.append(f'<span class="entity-ref">{escape_html(name)}</span>')
        last_end = end
    parts.append(escape_html(out[last_end:]))
    return "".join(parts)


# Bold: **word** -> <strong>word</strong>
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")


def sanitize_message_content(s: str) -> str:
    """Fix Unicode, entity markup (-> display name (type; desc)), and **word** -> <strong>. Returns HTML."""
    if not s:
        return s
    out = _fix_unicode_for_display(s)
    out = out.replace('\\"', '"')
    out = out.replace("&quot;", '"')  # already-escaped in heap
    out = out.replace("\u201c", '"').replace("\u201d", '"')  # curly quotes -> straight
    out_normalized = re.sub(r"[\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\ue200-\ue202]", "", out)
    if "entity[" in out_normalized:
        out = out_normalized  # always use normalized for entity matching
    out = _build_content_with_entity_spans(out)
    out = _BOLD_RE.sub(r"<strong>\1</strong>", out)
    return out


def _content_plain_and_entities(s: str) -> tuple[str, list[dict]]:
    """Return (plain text with entity->name, list of {type, name, description}) for JSON output."""
    if not s:
        return "", []
    out = _fix_unicode_for_display(s)
    out = out.replace('\\"', '"')
    matches = []
    for m in ENTITY_FULL_RE.finditer(out):
        matches.append((m.start(), m.end(), "3", (m.group(1), m.group(2), m.group(3))))
    for m in ENTITY_2_RE.finditer(out):
        if any(start <= m.start() < end for start, end, _, _ in matches):
            continue
        matches.append((m.start(), m.end(), "2", (m.group(1),)))
    matches.sort(key=lambda x: x[0])
    plain_parts = []
    entities = []
    last_end = 0
    for start, end, kind, grps in matches:
        plain_parts.append(out[last_end:start])
        if kind == "3":
            etype, name, desc = grps
            entities.append({"type": etype, "name": name, "description": desc})
            plain_parts.append(name)
        else:
            name = grps[0]
            entities.append({"type": "", "name": name, "description": ""})
            plain_parts.append(name)
        last_end = end
    plain_parts.append(out[last_end:])
    return "".join(plain_parts), entities


def node_summary_html(node: dict, edge_from_parent: dict | None = None) -> str:
    parts = []
    if edge_from_parent:
        parts.append(f"<span class=\"edge-label\">[{edge_from_parent.get('type', '')}] {escape_html(str(edge_from_parent.get('label', '')))} → </span>")
    parts.append(f"<span class=\"name\">{escape_html(node['name']) or '(empty name)'}</span> ")
    parts.append(type_badge(node["type"]))
    parts.append(f" <span class=\"meta\">node_index={node['node_index']}, id={node['id']}, self_size={node['self_size']}</span>")
    return "".join(parts)


def object_summary_html(
    snapshot: dict,
    part_of_key_child: dict,
    edge_from_parent: dict | None,
    edge_offsets: list[int] | None,
    fallback_uuid: str | None = None,
) -> str:
    """Summary line for structure report: show object's id (UUID) as label. Prefer UUID over 'client-created-root'."""
    parts = []
    if edge_from_parent:
        parts.append(f"<span class=\"edge-label\">[{edge_from_parent.get('type', '')}] {escape_html(str(edge_from_parent.get('label', '')))} → </span>")
    id_str = get_property_string(snapshot, part_of_key_child["node_index"], "id", edge_offsets)
    id_str = (id_str or "").strip()
    if id_str and UUID_PATTERN.search(id_str):
        label = id_str
    elif fallback_uuid and UUID_PATTERN.search(str(fallback_uuid)):
        label = str(fallback_uuid).strip()
    else:
        label = id_str or str(part_of_key_child.get("id", "")) or part_of_key_child.get("name") or "(no id)"
        label = str(label).strip()
    parts.append(f"<span class=\"name\">{escape_html(label)}</span> ")
    parts.append(type_badge(part_of_key_child["type"]))
    parts.append(f" <span class=\"meta\">node_index={part_of_key_child['node_index']}, self_size={part_of_key_child['self_size']}</span>")
    return "".join(parts)


def write_tree_depth_n(f, node: dict) -> None:
    edge = node.get("edge_from_parent")
    f.write("<li><details>\n<summary class=\"node\">")
    f.write(node_summary_html(node, edge))
    f.write("</summary>")
    if node.get("children"):
        f.write("\n<ul class=\"tree\">")
        for ch in node["children"]:
            write_tree_depth_n(f, ch)
        f.write("</ul>")
    f.write("\n</details></li>")


# 세션 구분: 연속 메시지 간 시간 간격이 이 값(초)을 넘으면 새 세션으로 분리
SESSION_GAP_SECONDS = 30 * 60  # 30분


def _parse_create_time_to_ts(raw: str | None) -> float | None:
    """Parse create_time raw value to Unix timestamp (seconds). None if unparseable."""
    if raw is None:
        return None
    raw = str(raw).strip()
    if not raw:
        return None
    try:
        ts = float(raw)
        if ts > 1e12:
            ts = ts / 1000.0
        elif ts < 0:
            return None
        return ts
    except ValueError:
        pass
    try:
        normalized = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized[:26])
        return dt.timestamp()
    except (ValueError, OSError):
        pass
    return None


def _split_ordered_messages_into_sessions(
    ordered: list[dict], gap_seconds: float = SESSION_GAP_SECONDS
) -> list[list[dict]]:
    """Ordered 메시지 리스트를 시간 간격으로 세션 단위로 나눔. gap 초과 시 새 세션."""
    if not ordered:
        return []
    sessions: list[list[dict]] = []
    current: list[dict] = [ordered[0]]
    prev_ts = _parse_create_time_to_ts(ordered[0].get("create_time"))
    for m in ordered[1:]:
        ts = _parse_create_time_to_ts(m.get("create_time"))
        if prev_ts is not None and ts is not None and (ts - prev_ts) > gap_seconds:
            sessions.append(current)
            current = [m]
        else:
            current.append(m)
        prev_ts = ts if ts is not None else prev_ts
    sessions.append(current)
    return sessions


def _format_display_time(raw: str | None) -> str:
    """Convert create_time.value raw value (e.g. timestamp) to readable date-time. Returns 'No time' if missing."""
    if raw is None:
        return "No time"
    raw = str(raw).strip()
    if not raw:
        return "No time"
    # Unix timestamp: convert from ms to seconds if needed, then format
    try:
        ts = float(raw)
        if ts > 1e12:
            ts = ts / 1000.0
        elif ts < 0:
            return raw
        dt = datetime.fromtimestamp(ts)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError):
        pass
    # ISO format string
    try:
        normalized = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized[:26])
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass
    return raw


def _build_message_record(
    snapshot: dict,
    msg_node_index: int,
    elements_tree: dict,
    edge_offsets: list[int] | None,
    object_node_index: int | None = None,
) -> dict:
    """Extract id, parentId, children, role, create_time, channel, content from a message node.
    Content: text only (asset/image blobs removed), consecutive dupes removed; tool role may use metadata title.
    Prefer full message.content.parts[*].text[*] so that long messages spanning multiple parts are not truncated."""
    role = get_author_role(snapshot, msg_node_index, edge_offsets)

    # 1순위: message.content.parts[*].text[*] 전체를 인덱스 순으로 모음
    strings_parts = get_all_text_from_message_parts(snapshot, msg_node_index, edge_offsets)

    # 2순위: elements 트리 기반 수집 (이전 방식) – 일부 포맷에서는 여기에만 전체 텍스트가 있는 경우가 있으므로
    strings_elements: list[str] = []
    if elements_tree:
        strings_elements = collect_strings_from_elements_tree(elements_tree)

    # 두 소스를 모두 합쳐서 사용 (순서를 보장하기 위해 단순 이어붙이기).
    # 약간의 중복은 허용하고, 실제 텍스트 정리는 _parse_content_parts 에 맡긴다.
    strings = []
    if strings_parts:
        strings.extend(strings_parts)
    if strings_elements:
        strings.extend(strings_elements)
    content, media_summary = _parse_content_parts(strings, role)
    tool_label_suffix = None
    tool_metadata_structured = None
    if role == "tool":
        author_info = get_author_display_info(snapshot, msg_node_index, edge_offsets)
        tool_meta = get_tool_metadata_summary(snapshot, msg_node_index, object_node_index, edge_offsets)
        content, tool_label_suffix = format_tool_content_and_label(author_info, tool_meta)
        tool_metadata_structured = {
            "author_name": author_info.get("name"),
            "real_author": author_info.get("real_author"),
            "source": author_info.get("source"),
            "search_queries": tool_meta.get("search_queries"),
            "search_result_groups": tool_meta.get("search_result_groups"),
            "image_title": tool_meta.get("image_title"),
            "status": tool_meta.get("status"),
        }
    else:
        if media_summary:
            content = (content + " " + media_summary).strip() if content else media_summary
    rec = {
        "id": get_property_string(snapshot, msg_node_index, "id", edge_offsets),
        "parentId": get_property_string(snapshot, msg_node_index, "parentId", edge_offsets),
        "children": get_children_ids_from_message(snapshot, msg_node_index, edge_offsets),
        "role": role,
        "create_time": get_create_time_value(snapshot, msg_node_index, edge_offsets),
        "channel": get_property_string(snapshot, msg_node_index, "channel", edge_offsets),
        "content": content.strip(),
    }
    if tool_label_suffix is not None:
        rec["tool_label_suffix"] = tool_label_suffix
    if tool_metadata_structured is not None:
        rec["tool_metadata"] = tool_metadata_structured
    return rec


def _get_root_id(msg: dict, by_id: dict[str, dict]) -> str | None:
    """Walk parentId chain to find the root in by_id; return that id. If we exit the set (parent not in by_id), return None so caller can assign table-level thread."""
    mid = msg.get("id")
    if not mid:
        return None
    visited = set()
    while mid:
        if mid in visited:
            return mid
        visited.add(mid)
        m = by_id.get(mid)
        if not m:
            return None
        pid = (m.get("parentId") or "").strip()
        if not pid or pid not in by_id:
            return None
        mid = pid
    return mid or None


def _group_messages_by_thread_root(messages: list[dict]) -> dict[str, list[dict]]:
    """Group messages by conversation root. If parentId chain leaves our set, treat whole table as one thread (synthetic key)."""
    by_id = {m["id"]: m for m in messages if m.get("id")}
    if not by_id:
        return {"": messages} if messages else {}
    root_to_msgs: dict[str, list[dict]] = {}
    synthetic_key = "__table_thread__"
    for m in messages:
        mid = m.get("id")
        if not mid:
            root_to_msgs.setdefault("", []).append(m)
            continue
        root_id = _get_root_id(m, by_id)
        if root_id is None:
            root_id = synthetic_key
        root_to_msgs.setdefault(root_id, []).append(m)
    return root_to_msgs


def _order_messages_by_parent_chain(messages: list[dict]) -> list[dict]:
    """Order messages using id/parentId/children as a dependency graph.
    - 부모(id/parentId 혹은 children 관계)는 항상 자식보다 먼저 나온다.
    - 같은 레벨에서는 create_time(숫자) -> id 순으로 정렬한다.
    이렇게 하면 일반적인 “유저 질문 → AI 답변 → 후속 질문 …” 순서가 보장된다."""
    by_id = {m["id"]: m for m in messages if m.get("id")}
    if not by_id:
        return sorted(messages, key=lambda m: (_parse_create_time_to_ts(m.get("create_time")) or float("inf"), m.get("id") or ""))

    children_map: dict[str, set[str]] = {mid: set() for mid in by_id}
    indegree: dict[str, int] = {mid: 0 for mid in by_id}
    seen_edges: set[tuple[str, str]] = set()

    def _add_edge(parent_id: str, child_id: str) -> None:
        if not parent_id or not child_id or parent_id == child_id:
            return
        if child_id not in by_id or parent_id not in by_id:
            return
        edge = (parent_id, child_id)
        if edge in seen_edges:
            return
        seen_edges.add(edge)
        children_map[parent_id].add(child_id)
        indegree[child_id] += 1

    for m in messages:
        mid = m.get("id")
        pid = m.get("parentId")
        if mid and pid:
            _add_edge(pid, mid)
    for m in messages:
        mid = m.get("id")
        if not mid:
            continue
        for cid in (m.get("children") or []):
            if cid:
                _add_edge(mid, cid)

    def _key(mid: str) -> tuple[float, str]:
        msg = by_id[mid]
        ts = _parse_create_time_to_ts(msg.get("create_time"))
        return (ts if ts is not None else float("inf"), mid)

    import heapq
    heap: list[tuple[float, str]] = []
    for mid, deg in indegree.items():
        if deg == 0:
            heapq.heappush(heap, _key(mid))

    ordered_ids: list[str] = []
    while heap:
        _, mid = heapq.heappop(heap)
        ordered_ids.append(mid)
        for cid in children_map.get(mid, ()):
            indegree[cid] -= 1
            if indegree[cid] == 0:
                heapq.heappush(heap, _key(cid))

    remaining = [mid for mid in by_id.keys() if mid not in ordered_ids]
    remaining.sort(key=_key)
    ordered_ids.extend(remaining)

    ordered = [by_id[mid] for mid in ordered_ids]

    no_id = [m for m in messages if not m.get("id")]
    if no_id:
        no_id.sort(key=lambda m: (_parse_create_time_to_ts(m.get("create_time")) or float("inf"), m.get("role") or "", m.get("content") or ""))
        ordered.extend(no_id)
    return ordered


def _conversation_css() -> str:
    return (
        "body { font-family: 'Segoe UI', system-ui, sans-serif; margin: 0; padding: 2rem 3rem; background: #ffffff; color: #1a1a1a; line-height: 1.5; }\n"
        ".conv-header { margin-bottom: 2rem; padding-bottom: 1rem; border-bottom: 1px solid #e0e0e0; }\n"
        ".conv-header h1 { margin: 0 0 0.25rem 0; font-size: 1.5rem; font-weight: 600; color: #1a1a1a; }\n"
        ".conv-header .sub { font-size: 0.875rem; color: #666; }\n"
        ".thread { margin-bottom: 2.5rem; padding: 1.5rem; background: #fafafa; border-radius: 8px; border: 1px solid #e8e8e8; }\n"
        ".thread-title { font-size: 0.8125rem; font-weight: 600; color: #666; margin-bottom: 1rem; }\n"
        ".chat { display: flex; flex-direction: column; gap: 0.75rem; }\n"
        ".msg { max-width: 82%; padding: 12px 16px; border-radius: 10px; line-height: 1.5; word-break: break-word; }\n"
        ".msg.user { align-self: flex-end; background: #e8e8e8; color: #1a1a1a; border-bottom-right-radius: 4px; }\n"
        ".msg.assistant { align-self: flex-start; background: #f0f0f0; color: #1a1a1a; border: 1px solid #e0e0e0; border-bottom-left-radius: 4px; }\n"
        ".msg.tool { align-self: flex-start; background: #f5f5f5; color: #333; border: 1px solid #e0e0e0; border-bottom-left-radius: 4px; }\n"
        ".msg .role { font-size: 0.6875rem; text-transform: uppercase; letter-spacing: 0.03em; opacity: 0.85; margin-bottom: 4px; color: #555; }\n"
        ".msg .time { font-size: 0.75rem; margin-top: 6px; color: #888; }\n"
        ".msg .content { white-space: pre-wrap; font-family: 'Segoe UI', 'Apple Color Emoji', 'Segoe UI Emoji', 'Noto Color Emoji', sans-serif; }\n"
        ".msg .content .entity-ref { border-bottom: none; cursor: default; }\n"
        ".msg .content .entity-desc { font-size: 0.9em; color: #666; }\n"
    )


def generate_conversation_html(
    snapshot: dict, uuid_entries: list, edge_offsets: list[int] | None, out_path: str
) -> None:
    """세션 = (weakmap, table, part_of_key_child) 단위. 세션별로 블록을 나누고, 같은 세션 내에서는 id/parentId 순서로 메시지 표시."""
    key_to_entries: dict[tuple, list] = {}
    for entry in uuid_entries:
        if len(entry) < 5 or entry[4] is None:
            continue
        weakmap_node, table_node, part_of_key_child, uuid_tree, msg_parts_elements = entry[:5]
        key = (weakmap_node["node_index"], table_node["node_index"], part_of_key_child["node_index"])
        key_to_entries.setdefault(key, []).append(entry)
    if not key_to_entries:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("<!DOCTYPE html><html><body><p>No conversation data (message path not found).</p></body></html>")
        return
    thread_blocks: list[tuple[float, str]] = []
    for _key, entries in key_to_entries.items():
        messages = []
        seen_ids = set()
        for ent in entries:
            _, _, part_of_key_child, _, msg_parts_elements = ent[:5]
            if msg_parts_elements is None:
                continue
            path_chain, elements_tree = msg_parts_elements
            msg_idx = path_chain[0][0]["node_index"]
            obj_idx = part_of_key_child.get("node_index") if part_of_key_child else None
            rec = _build_message_record(snapshot, msg_idx, elements_tree, edge_offsets, object_node_index=obj_idx)
            if rec.get("id") and rec["id"] not in seen_ids:
                seen_ids.add(rec["id"])
                messages.append(rec)
        ordered = _order_messages_by_parent_chain(messages)
        if not ordered:
            ordered = messages
        bubbles = []
        for m in ordered:
            content_raw = (m.get("content") or "").strip()
            if content_raw is None or content_raw == "":
                continue
            content = sanitize_message_content(content_raw)
            if (m.get("channel") or "").strip().lower() == "commentary":
                content += " [System/storage]"
            role_label = "User" if m.get("role") == "user" else ("Tool (" + m.get("tool_label_suffix", "tool") + ")" if m.get("role") == "tool" else "Assistant (ChatGPT)")
            time_display = _format_display_time(m.get("create_time"))
            bubbles.append(
                f'<div class="msg {m.get("role", "unknown")}">'
                f'<div class="role">{role_label}</div>'
                f'<div class="content">{content}</div>'
                f'<div class="time">Time: {escape_html(time_display)}</div></div>'
            )
        if not bubbles:
            continue
        time_range = ""
        if ordered:
            times = [_format_display_time(m.get("create_time")) for m in ordered]
            valid_times = [t for t in times if t != "No time"]
            if valid_times:
                time_range = f" — {valid_times[0]} – {valid_times[-1]}"
        latest_ts = 0.0
        for m in ordered:
            t = _parse_create_time_to_ts(m.get("create_time"))
            if t is not None and t > latest_ts:
                latest_ts = t
        block = (
            '<div class="thread">'
            f'<div class="thread-title">Session{escape_html(time_range) if time_range else ""}</div>'
            f'<div class="chat">{"".join(bubbles)}</div></div>'
        )
        thread_blocks.append((latest_ts, block))
    thread_blocks.sort(key=lambda x: x[0], reverse=True)
    threads_html = [b for _, b in thread_blocks]
    html = (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"UTF-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">\n"
        "<title>Conversation Threads — Heap Snapshot Forensics</title>\n<style>\n" + _conversation_css() + "</style>\n</head>\n<body>\n"
        "<header class=\"conv-header\"><h1>Conversation Threads</h1>\n<p class=\"sub\">Ordered by thread and message sequence</p></header>\n"
        + "\n".join(threads_html) + "\n</body></html>"
    )
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)


def generate_conversation_json(
    snapshot: dict, uuid_entries: list, edge_offsets: list[int] | None, out_path: str
) -> None:
    """세션 = (weakmap, table, part_of_key_child) 단위. HTML과 동일한 세션 구분으로 JSON 출력."""
    key_to_entries: dict[tuple, list] = {}
    for entry in uuid_entries:
        if len(entry) < 5 or entry[4] is None:
            continue
        weakmap_node, table_node, part_of_key_child, uuid_tree, msg_parts_elements = entry[:5]
        key = (weakmap_node["node_index"], table_node["node_index"], part_of_key_child["node_index"])
        key_to_entries.setdefault(key, []).append(entry)
    if not key_to_entries:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"threads": [], "message": "No conversation data (message path not found)."}, f, ensure_ascii=False, indent=2)
        return
    thread_list: list[tuple[float, dict]] = []
    for _key, entries in key_to_entries.items():
        messages = []
        seen_ids = set()
        for ent in entries:
            _, _, part_of_key_child, _, msg_parts_elements = ent[:5]
            if msg_parts_elements is None:
                continue
            path_chain, elements_tree = msg_parts_elements
            msg_idx = path_chain[0][0]["node_index"]
            obj_idx = part_of_key_child.get("node_index") if part_of_key_child else None
            rec = _build_message_record(snapshot, msg_idx, elements_tree, edge_offsets, object_node_index=obj_idx)
            if rec.get("id") and rec["id"] not in seen_ids:
                seen_ids.add(rec["id"])
                messages.append(rec)
        ordered = _order_messages_by_parent_chain(messages)
        if not ordered:
            ordered = messages
        session_start = None
        session_end = None
        out_messages = []
        latest_ts = 0.0
        for m in ordered:
            t = _parse_create_time_to_ts(m.get("create_time"))
            if t is not None and t > latest_ts:
                latest_ts = t
            content_raw = (m.get("content") or "").strip()
            if not content_raw:
                continue
            content_plain, entities = _content_plain_and_entities(content_raw)
            if (m.get("channel") or "").strip().lower() == "commentary":
                content_plain += " [System/storage]"
            role = m.get("role", "unknown")
            role_label = "User" if role == "user" else ("Tool (" + m.get("tool_label_suffix", "tool") + ")" if role == "tool" else "Assistant (ChatGPT)")
            time_display = _format_display_time(m.get("create_time"))
            msg_obj = {
                "role": role,
                "role_label": role_label,
                "content": content_plain,
                "time": time_display,
            }
            if entities:
                msg_obj["related_info"] = entities
            if m.get("tool_metadata"):
                msg_obj["tool_metadata"] = m["tool_metadata"]
            out_messages.append(msg_obj)
            if time_display != "No time":
                if session_start is None:
                    session_start = session_end = time_display
                else:
                    session_end = time_display
        if out_messages:
            thread_list.append((latest_ts, {
                "session_start": session_start,
                "session_end": session_end,
                "messages": out_messages,
            }))
    thread_list.sort(key=lambda x: x[0], reverse=True)
    threads = [t for _, t in thread_list]
    payload = {"threads": threads}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _common_css() -> str:
    return (
        "body { font-family: 'Segoe UI', system-ui, sans-serif; margin: 0; padding: 2rem 3rem; background: #0f1419; color: #e6edf3; line-height: 1.5; }\n"
        ".report-header { margin-bottom: 2rem; padding-bottom: 1rem; border-bottom: 1px solid #30363d; }\n"
        ".report-header h1 { margin: 0 0 0.25rem 0; font-size: 1.5rem; font-weight: 600; color: #e6edf3; }\n"
        ".report-meta { margin: 0; font-size: 0.875rem; color: #8b949e; }\n"
        ".badge { display: inline-block; padding: 2px 6px; border-radius: 4px; color: #fff; font-size: 11px; }\n"
        ".node { margin: 6px 0; padding: 10px 14px; background: #161b22; border-radius: 6px; border-left: 3px solid #58a6ff; }\n"
        ".node.node-missing { border-left-color: #f85149; color: #8b949e; }\n"
        ".node.node-message-text { border-left-color: #7ee787; }\n"
        ".node.node-nested { opacity: 0.9; }\n"
        "details .node { margin-left: 0; }\n.node .name { font-weight: 600; color: #f0f6fc; }\n.node .meta { font-size: 11px; color: #8b949e; }\n"
        ".edge-label { font-size: 11px; color: #58a6ff; }\n"
        "details { margin: 4px 0; }\ndetails > summary { list-style: none; cursor: pointer; }\n"
        "details > summary::-webkit-details-marker { display: none; }\n"
        "details > summary::before { content: '▶ '; font-size: 10px; }\n"
        "details[open] > summary::before { content: '▼ '; }\n"
        "ul.tree { list-style: none; padding-left: 1.25rem; margin: 6px 0; }\n"
    )


def generate_html_weakmaps(
    snapshot: dict,
    uuid_only_path: str,
    write_structure_report: bool = True,
    write_conversation: bool = True,
) -> tuple[int, int]:
    """Generate UUID-only and/or conversation HTML. Returns (uuid_entries_count, message_path_count)."""
    weakmap_indices = find_all_nodes_by_exact_name(snapshot, "WeakMap")
    if not weakmap_indices:
        if write_structure_report:
            with open(uuid_only_path, "w", encoding="utf-8") as f:
                f.write("<!DOCTYPE html><html><body><p>WeakMap (exact) not found.</p></body></html>")
        return 0, 0
    edge_offsets = get_edge_offsets(snapshot)
    entries = []
    uuid_entries = []
    for idx in weakmap_indices:
        node = get_node(snapshot, idx)
        table_index, table_edge = find_child_by_exact_name_and_edge(snapshot, idx, "table", edge_offsets)
        if table_index is None:
            continue
        table_node = get_node(snapshot, table_index)
        table_tree = build_depth1_tree(snapshot, table_index, edge_offsets)
        entries.append((node, table_node, table_edge, table_tree))
    if not entries:
        if write_structure_report:
            with open(uuid_only_path, "w", encoding="utf-8") as f:
                f.write("<!DOCTYPE html><html><body><p>WeakMap with child 'table' not found.</p></body></html>")
        return 0, 0
    seen_uuid_node_index = set()
    for weakmap_node, table_node, table_edge, table_tree in entries:
        for child in table_tree["children"]:
            edge = child.get("edge_from_parent", {})
            edge_label = str(edge.get("label", ""))
            is_part_of_key_object = child.get("type") == "object" and "part of key" in edge_label
            has_structure = is_part_of_key_object and object_has_id_parentid_children_message_structure(
                snapshot, child["node_index"], edge_offsets
            )
            if not has_structure:
                continue
            obj_tree = build_depth1_tree(snapshot, child["node_index"], edge_offsets)
            for grandchild in obj_tree["children"]:
                gedge = grandchild.get("edge_from_parent", {})
                g_label = str(gedge.get("label", ""))
                g_name = grandchild.get("name", "")
                if not (contains_uuid(g_label) or contains_uuid(g_name)):
                    continue
                uid = grandchild["node_index"]
                if uid in seen_uuid_node_index:
                    continue
                seen_uuid_node_index.add(uid)
                m = UUID_PATTERN.search(g_label or "")
                uuid_str = m.group(0) if m else (UUID_PATTERN.search(g_name or "").group(0) if UUID_PATTERN.search(g_name or "") else None)
                uuid_tree = build_depth_n_tree(snapshot, uid, 4, edge_offsets)
                obj_with_props = get_object_with_required_props(snapshot, child["node_index"], edge_offsets)
                msg_from_obj = get_message_content_parts_elements_tree(snapshot, obj_with_props, edge_offsets) if obj_with_props >= 0 else None
                msg_from_uuid = get_message_content_parts_elements_tree(snapshot, uid, edge_offsets)
                # 각 UUID별로 해당 노드의 메시지를 쓰기 위해 UUID 노드 메시지를 우선 사용 (다른 ID인데 같은 메시지가 나오는 것 방지)
                msg_parts_elements = msg_from_uuid if msg_from_uuid is not None else msg_from_obj
                uuid_entries.append((weakmap_node, table_node, child, uuid_tree, msg_parts_elements, uuid_str))
    found_message_path = sum(1 for e in uuid_entries if len(e) > 4 and e[4] is not None)
    out_dir = os.path.dirname(uuid_only_path)
    if write_conversation:
        conversation_path = os.path.join(out_dir, "conversation_threads.html")
        conversation_json_path = os.path.join(out_dir, "conversation_threads.json")
        generate_conversation_html(snapshot, uuid_entries, edge_offsets, conversation_path)
        generate_conversation_json(snapshot, uuid_entries, edge_offsets, conversation_json_path)
    if not write_structure_report:
        return len(uuid_entries), found_message_path
    header_uuid = (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"UTF-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">\n"
        "<title>Structure Report — Heap Snapshot Forensics</title>\n<style>\n" + _common_css() + "</style>\n</head>\n<body>\n"
        "<header class=\"report-header\"><h1>Structure Report</h1>\n"
        f"<p class=\"report-meta\">{len(uuid_entries)} objects · {found_message_path} with message content</p></header>\n"
    )
    with open(uuid_only_path, "w", encoding="utf-8") as f:
        f.write(header_uuid)
        for entry in uuid_entries:
            weakmap_node, table_node, part_of_key_child, uuid_tree = entry[:4]
            msg_parts_elements = entry[4] if len(entry) > 4 else None
            uuid_str = entry[5] if len(entry) > 5 else None
            # Root = part of key object (table → "part of key" → full subtree from here)
            root_idx = part_of_key_child["node_index"]
            root_node = get_node(snapshot, root_idx)
            fallback_uuid = uuid_str or (str(uuid_tree.get("name") or "") if uuid_tree else None)
            # 1) Root: part of key — title = UUID for identification
            f.write("<details>\n<summary class=\"node\">")
            f.write(object_summary_html(snapshot, root_node, None, edge_offsets, fallback_uuid=fallback_uuid))
            f.write("</summary>\n<ul class=\"tree\">")
            # 2) Only the branch for this entry: part of key → [key=UUID] → conversation object subtree
            uid = uuid_tree["node_index"] if uuid_tree else -1
            part_of_key_tree = build_depth_n_tree(snapshot, root_idx, 4, edge_offsets)
            for ch in part_of_key_tree.get("children", []):
                if ch.get("node_index") == uid:
                    write_tree_depth_n(f, ch)
                    break
            # 3) Message text summary line for quick read
            if msg_parts_elements is not None:
                path_chain, elements_tree = msg_parts_elements
                strings = collect_strings_from_elements_tree(elements_tree)
                text = " ".join(s for s in strings if s and s.strip()).strip()
                if not text:
                    text = "(no text)"
                f.write("<li class=\"node node-message-text\"><span class=\"meta\">Message: </span>")
                f.write(escape_html(text))
                f.write("</li>")
            else:
                f.write("<li class=\"node node-missing\">Message path not found for this object.</li>")
            f.write("</ul>\n</details>\n")
        f.write("</body></html>")
    return len(uuid_entries), found_message_path


def run_analysis(
    snapshot_path: str,
    output_dir: str | None = None,
    generate_structure_report: bool = True,
) -> dict:
    """
    Load a heap snapshot, run WeakMap/UUID and conversation extraction, write HTML reports.
    When generate_structure_report is False, only conversation_threads are written (structure_report on demand).
    Returns dict with keys: uuid_only_path, conversation_path, snapshot_path, error (if any).
    """
    if output_dir is None:
        output_dir = os.path.dirname(os.path.abspath(snapshot_path))
    os.makedirs(output_dir, exist_ok=True)
    uuid_only_path = os.path.join(output_dir, "structure_report.html")
    conversation_path = os.path.join(output_dir, "conversation_threads.html")
    conversation_json_path = os.path.join(output_dir, "conversation_threads.json")
    result = {
        "snapshot_path": snapshot_path,
        "uuid_only_path": uuid_only_path,
        "conversation_path": conversation_path,
        "conversation_json_path": conversation_json_path,
        "error": None,
        "weakmap_count": 0,
        "uuid_entries_count": 0,
        "message_path_count": 0,
    }
    try:
        snapshot = load_snapshot(snapshot_path)
        num_entries, num_messages = generate_html_weakmaps(
            snapshot,
            uuid_only_path,
            write_structure_report=generate_structure_report,
            write_conversation=True,
        )
        result["uuid_entries_count"] = num_entries
        result["message_path_count"] = num_messages
        result["weakmap_count"] = len(find_all_nodes_by_exact_name(snapshot, "WeakMap"))
    except Exception as e:
        result["error"] = str(e)
    return result


def generate_structure_report(snapshot_path: str, output_dir: str) -> dict:
    """
    Load snapshot and generate only structure_report.html (on-demand).
    Returns dict with uuid_only_path and error (if any).
    """
    uuid_only_path = os.path.join(output_dir, "structure_report.html")
    result = {"uuid_only_path": uuid_only_path, "error": None}
    try:
        os.makedirs(output_dir, exist_ok=True)
        snapshot = load_snapshot(snapshot_path)
        generate_html_weakmaps(
            snapshot,
            uuid_only_path,
            write_structure_report=True,
            write_conversation=False,
        )
    except Exception as e:
        result["error"] = str(e)
    return result


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if len(sys.argv) >= 2:
        snapshot_path = os.path.abspath(sys.argv[1])
    else:
        snapshot_path = os.path.join(script_dir, "Heap1.heapsnapshot")
    if not os.path.isfile(snapshot_path):
        print("File not found:", snapshot_path)
        sys.exit(2)
    result = run_analysis(snapshot_path, script_dir)
    if result.get("error"):
        print("Error:", result["error"])
        sys.exit(1)
    print("Done:", result["uuid_only_path"])
    print("Conversation HTML:", result["conversation_path"])
    print("Conversation JSON:", result["conversation_json_path"])
    print("(See HTML header for message->content->parts->elements count; 0 = path not in heap or different names)")


if __name__ == "__main__":
    main()
