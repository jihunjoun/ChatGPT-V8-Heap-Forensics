# -*- coding: utf-8 -*-
"""
Heap Snapshot parser: WeakMap -> table, only nodes with id/parentId/children/message.
Generates structure_report.html, conversation_threads.html/json, and a forensic_run_summary.txt.
"""

import hashlib
import json
import os
import platform
import re
import sys
import time
from datetime import datetime, timezone

NODE_FIELD_COUNT = 6
EDGE_FIELD_COUNT = 3
UUID_PATTERN = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# Synthetic root id shared across conversation branches in the ChatGPT client (many wrapper instances may exist in the heap).
CLIENT_CREATED_ROOT = "client-created-root"
# Structure report — message property subtree depth: base + extra depth for content/metadata.
STRUCTURE_MESSAGE_PROP_DEPTH = 2
STRUCTURE_MESSAGE_CONTENT_EXTRA_DEPTH = 1
STRUCTURE_MESSAGE_METADATA_EXTRA_DEPTH = 2
STRUCTURE_MESSAGE_PROPS_FIRST = ("author", "create_time", "content", "metadata")
# Depth under the wrapper's `children` property node (+1 hop vs earlier default).
STRUCTURE_WRAPPER_CHILDREN_DEPTH = 2

TOOL_VERSION = "1.0"
FORENSIC_RUN_SUMMARY_FILENAME = "forensic_run_summary.txt"


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


def get_children_ids_from_owner(snapshot: dict, owner_node_index: int, edge_offsets: list[int] | None) -> list[str]:
    """Resolve child message ids from `children` on a conversation wrapper or legacy message node.

    - Legacy: `children` is an array of objects each with `.id`.
    - ChatGPT 5.x style: `children` is an object with internal `elements` array of UUID strings,
      or numeric element edges directly to string nodes.
    """
    children_idx = get_property_node(snapshot, owner_node_index, "children", edge_offsets)
    if children_idx is None:
        return []
    node = get_node(snapshot, children_idx)
    ct = node.get("type")
    if ct not in ("array", "object"):
        return []

    if ct == "object":
        elements_idx = get_property_node(snapshot, children_idx, "elements", edge_offsets)
        if elements_idx is not None and get_node(snapshot, elements_idx).get("type") == "array":
            parts = _collect_array_strings(snapshot, elements_idx, edge_offsets)
            return [x.strip() for x in parts if x and str(x).strip()]
        ids: list[str] = []
        seen: set[str] = set()
        for e in get_edges_from_node(snapshot, children_idx, edge_offsets):
            lab = str(e.get("label", "")).strip()
            if not lab.isdigit():
                continue
            child_idx = e["to_node_index"]
            child_node = get_node(snapshot, child_idx)
            t = child_node.get("type") or ""
            if t in ("string", "hidden", "concatenated string", "sliced string", "synthetic"):
                s = _node_name_as_string(snapshot, child_node)
                if s and s.strip() and s not in seen:
                    seen.add(s)
                    ids.append(s.strip())
            elif t == "object":
                sid = get_property_string(snapshot, child_idx, "id", edge_offsets)
                if sid and sid not in seen:
                    seen.add(sid)
                    ids.append(sid)
        if ids:
            return ids

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


def get_children_ids_from_message(snapshot: dict, message_node_index: int, edge_offsets: list[int] | None) -> list[str]:
    """Return child message ids from the message node's `children` (same resolution as conversation wrapper)."""
    return get_children_ids_from_owner(snapshot, message_node_index, edge_offsets)


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

    # Walk parts[0], parts[1], ... in numeric index order.
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
        # 1) part.text array present
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

        # 2) Fallback: part.elements[*].text[*] style structure
        elements_idx = get_property_node(snapshot, part_idx, "elements", edge_offsets)
        if elements_idx is not None:
            part_strings = _collect_array_strings(snapshot, elements_idx, edge_offsets)
            result.extend(part_strings)
            continue

        # 3) Last resort: first string / name on the part object
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
        for _, group_idx in _iter_array_like_children(snapshot, srg_idx, edge_offsets):
            group_node = get_node(snapshot, group_idx)
            if group_node.get("type") not in ("object", "array"):
                continue
            props = _get_object_string_props(snapshot, group_idx, edge_offsets)
            domain = props.get("domain") or _get_property_string_or_name(snapshot, group_idx, "domain", edge_offsets) or ""
            entries_idx = get_property_node(snapshot, group_idx, "entries", edge_offsets)
            entries = []
            if entries_idx is not None:
                for _, ent_idx in _iter_array_like_children(snapshot, entries_idx, edge_offsets):
                    ent_props = _get_object_string_props(snapshot, ent_idx, edge_offsets)
                    title = ent_props.get("title") or _get_property_string_or_name(snapshot, ent_idx, "title", edge_offsets) or ""
                    url = ent_props.get("url") or _get_property_string_or_name(snapshot, ent_idx, "url", edge_offsets) or ""
                    snippet = ent_props.get("snippet") or _get_property_string_or_name(snapshot, ent_idx, "snippet", edge_offsets) or ""
                    attr = ent_props.get("attribution") or get_property_string(snapshot, ent_idx, "attribution", edge_offsets) or ""
                    if not any(
                        ((title or "").strip(), (url or "").strip(), (snippet or "").strip(), (attr or "").strip())
                    ):
                        continue
                    entries.append(
                        {
                            "title": title,
                            "url": url,
                            "snippet": (snippet or "").strip(),
                            "attribution": (attr or "").strip(),
                        }
                    )
            if domain or entries:
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


def get_entry_message_id_for_threading(snapshot: dict, entry: tuple, edge_offsets: list[int] | None) -> str | None:
    """message.id inside a wrapper entry (for thread assignment)."""
    if len(entry) < 3:
        return None
    widx = entry[2]["node_index"]
    msg_idx = get_property_node(snapshot, widx, "message", edge_offsets)
    if msg_idx is None:
        return None
    return get_property_string(snapshot, msg_idx, "id", edge_offsets)


def _structure_depth_for_message_prop(prop: str) -> int:
    d = STRUCTURE_MESSAGE_PROP_DEPTH
    if prop == "content":
        return d + STRUCTURE_MESSAGE_CONTENT_EXTRA_DEPTH
    if prop == "metadata":
        return d + STRUCTURE_MESSAGE_METADATA_EXTRA_DEPTH
    return d


def _collect_message_property_children(snapshot: dict, msg_idx: int, edge_offsets: list[int] | None) -> list[tuple[str, int]]:
    """(property label, child node_index) pairs for a message object; STRUCTURE_MESSAGE_PROPS_FIRST first, then alphabetical."""
    by_label: dict[str, int] = {}
    for e in get_edges_from_node(snapshot, msg_idx, edge_offsets):
        if e.get("type") not in ("property", "internal"):
            continue
        lab = str(e.get("label", "")).strip()
        if not lab:
            continue
        cidx = e["to_node_index"]
        if lab not in by_label:
            by_label[lab] = cidx
    out: list[tuple[str, int]] = []
    for p in STRUCTURE_MESSAGE_PROPS_FIRST:
        if p in by_label:
            out.append((p, by_label.pop(p)))
    for lab in sorted(by_label.keys()):
        out.append((lab, by_label[lab]))
    return out


def write_message_core_subtree_html(
    f,
    snapshot: dict,
    msg_idx: int,
    edge_offsets: list[int] | None,
) -> None:
    """Message node: render all properties; content/metadata get extra depth."""
    msg_node = get_node(snapshot, msg_idx)
    f.write("<li><details>\n<summary class=\"node\">")
    f.write(
        node_summary_html(
            msg_node,
            {"type": "property", "label": "message"},
        )
    )
    f.write("</summary>\n<ul class=\"tree\">")
    for prop, pidx in _collect_message_property_children(snapshot, msg_idx, edge_offsets):
        depth = _structure_depth_for_message_prop(prop)
        subtree = build_depth_n_tree(snapshot, pidx, depth, edge_offsets)
        subtree["edge_from_parent"] = {"type": "property", "label": prop}
        write_tree_depth_n(f, subtree)
    f.write("</ul>\n</details></li>")


def write_structure_report_wrapper_subtree(
    f,
    snapshot: dict,
    wrapper_idx: int,
    edge_offsets: list[int] | None,
) -> None:
    """Wrapper to depth 1: full rules for `message`, deeper for `children`, depth 1 for other properties."""
    f.write('<ul class="tree">')
    wtree = build_depth_n_tree(snapshot, wrapper_idx, 1, edge_offsets)
    for ch in wtree.get("children", []):
        edge = ch.get("edge_from_parent") or {}
        lab = str(edge.get("label", "")).strip().lower()
        if lab == "message":
            write_message_core_subtree_html(f, snapshot, ch["node_index"], edge_offsets)
        elif lab == "children":
            sub = build_depth_n_tree(snapshot, ch["node_index"], STRUCTURE_WRAPPER_CHILDREN_DEPTH, edge_offsets)
            sub["edge_from_parent"] = edge
            write_tree_depth_n(f, sub)
        else:
            write_tree_depth_n(f, ch)
    f.write("</ul>")


def _structure_thread_section_title(comp: list[dict]) -> str:
    """Section title from stem id (direct under root) or first id in the component."""
    for m in comp:
        if m.get("id") and (m.get("parentId") or "").strip() == CLIENT_CREATED_ROOT:
            return str(m["id"])
    if comp and comp[0].get("id"):
        return str(comp[0]["id"])
    return "thread"


def _partition_structure_report_entries(
    snapshot: dict, uuid_entries: list, edge_offsets: list[int] | None
) -> tuple[list[int], list[list], list, list[list[dict]]]:
    """Partition entries by thread for structure_report HTML."""
    if not uuid_entries:
        return [], [], [], []
    all_msgs = collect_unique_message_records_from_entries(snapshot, uuid_entries, edge_offsets)
    components = cluster_messages_into_threads(all_msgs)
    mid_to_thread: dict[str, int] = {}
    for ti, comp in enumerate(components):
        for m in comp:
            if m.get("id"):
                mid_to_thread[m["id"]] = ti
    thread_entries: list[list] = [[] for _ in components]
    orphan_entries: list = []
    for ent in uuid_entries:
        mid = get_entry_message_id_for_threading(snapshot, ent, edge_offsets)
        if mid and mid in mid_to_thread:
            thread_entries[mid_to_thread[mid]].append(ent)
        else:
            orphan_entries.append(ent)
    thread_order = sorted(
        range(len(components)),
        key=lambda i: _component_latest_ts(components[i]) if components[i] else 0.0,
        reverse=True,
    )
    return thread_order, thread_entries, orphan_entries, components


def _structure_entry_extracted_text(snapshot: dict, entry: tuple, edge_offsets: list[int] | None) -> str | None:
    if len(entry) < 5 or entry[4] is None:
        return None
    msg_parts_elements = entry[4]
    path_chain, elements_tree = msg_parts_elements
    msg_idx = path_chain[0][0]["node_index"] if path_chain else None
    text_parts: list[str] = []
    if msg_idx is not None:
        text_parts.extend(get_all_text_from_message_parts(snapshot, msg_idx, edge_offsets))
    if elements_tree:
        text_parts.extend(collect_strings_from_elements_tree(elements_tree))
    text, media_summary = _parse_content_parts(text_parts, "unknown")
    if media_summary:
        text = (text + " " + media_summary).strip() if text else media_summary
    t = (text or "").strip()
    return t if t else None


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


def _indexed_children_sorted(
    snapshot: dict, parent_idx: int, edge_offsets: list[int] | None
) -> list[tuple[int, int]]:
    pairs: list[tuple[int, int]] = []
    for e in get_edges_from_node(snapshot, parent_idx, edge_offsets):
        lab = str(e.get("label", "")).strip()
        if lab.isdigit():
            pairs.append((int(lab), e["to_node_index"]))
    pairs.sort(key=lambda x: x[0])
    return pairs


def _iter_array_like_children(
    snapshot: dict, parent_idx: int, edge_offsets: list[int] | None
) -> list[tuple[int, int]]:
    """Children indexed 0,1,... — either a true array node or object.parts.elements (V8 heap)."""
    n = get_node(snapshot, parent_idx)
    nt = (n.get("type") or "").lower()
    if nt == "array":
        return _indexed_children_sorted(snapshot, parent_idx, edge_offsets)
    el = get_property_node(snapshot, parent_idx, "elements", edge_offsets)
    if el is not None and (get_node(snapshot, el).get("type") or "").lower() == "array":
        return _indexed_children_sorted(snapshot, el, edge_offsets)
    return _indexed_children_sorted(snapshot, parent_idx, edge_offsets)


def _extract_content_flat_text(snapshot: dict, content_idx: int, edge_offsets: list[int] | None) -> str | None:
    """message.content.text as a string or concatenated array elements."""
    text_idx = get_property_node(snapshot, content_idx, "text", edge_offsets)
    if text_idx is None:
        return None
    node = get_node(snapshot, text_idx)
    t = (node.get("type") or "").lower()
    if t in ("array", "object"):
        parts = _collect_array_strings(snapshot, text_idx, edge_offsets)
        return "\n".join(parts) if parts else None
    s = _node_name_as_string(snapshot, node)
    if s:
        return s
    return get_property_string(snapshot, content_idx, "text", edge_offsets)


def _extract_message_parts_detail(
    snapshot: dict, parts_idx: int, edge_offsets: list[int] | None
) -> list[dict]:
    """message.content.parts[n]: height, width, size_bytes, string, metadata.*."""
    out: list[dict] = []
    for i, part_idx in _iter_array_like_children(snapshot, parts_idx, edge_offsets):
        row: dict = {"index": i}
        pn = get_node(snapshot, part_idx)
        pt = (pn.get("type") or "").lower()
        if pt in ("string", "concatenated string", "sliced string", "hidden"):
            s0 = _node_name_as_string(snapshot, pn)
            if s0:
                row["string"] = s0
        for prop in ("height", "width", "size_bytes"):
            v = get_property_number(snapshot, part_idx, prop, edge_offsets)
            if v is not None:
                row[prop] = v
        s = get_property_string(snapshot, part_idx, "string", edge_offsets)
        if s:
            row["string"] = s
        if "string" not in row and get_property_node(snapshot, part_idx, "string", edge_offsets) is None:
            if pt == "string":
                ns = _node_name_as_string(snapshot, pn)
                if ns:
                    row["string"] = ns
        meta_idx = get_property_node(snapshot, part_idx, "metadata", edge_offsets)
        if meta_idx is not None:
            md: dict = {}
            d_idx = get_property_node(snapshot, meta_idx, "dalle", edge_offsets)
            if d_idx is not None:
                gid = get_property_string(snapshot, d_idx, "gen_id", edge_offsets)
                if gid:
                    md["dalle"] = {"gen_id": gid}
            gen_idx = get_property_node(snapshot, meta_idx, "generation", edge_offsets)
            if gen_idx is not None:
                gid = get_property_string(snapshot, gen_idx, "gen_id", edge_offsets)
                if gid:
                    md["generation"] = {"gen_id": gid}
            san = get_property_string(snapshot, meta_idx, "sanitized", edge_offsets)
            if san:
                md["sanitized"] = san
            if md:
                row["metadata"] = md
        out.append(row)
    return out


def _extract_attachments_list(snapshot: dict, meta_idx: int, edge_offsets: list[int] | None) -> list[dict]:
    att_idx = get_property_node(snapshot, meta_idx, "attachments", edge_offsets)
    if att_idx is None:
        return []
    rows: list[dict] = []
    for _, node_idx in _iter_array_like_children(snapshot, att_idx, edge_offsets):
        item: dict = {}
        for k in ("height", "width", "size", "id", "name", "source"):
            if k in ("height", "width", "size"):
                v = get_property_number(snapshot, node_idx, k, edge_offsets)
            else:
                v = get_property_string(snapshot, node_idx, k, edge_offsets)
            if v is not None and v != "":
                item[k] = v
        props = _get_object_string_props(snapshot, node_idx, edge_offsets)
        for k, v in props.items():
            if k not in item and v:
                item[k] = v
        if item:
            rows.append(item)
    return rows


def _extract_content_references_list(snapshot: dict, meta_idx: int, edge_offsets: list[int] | None) -> list[dict]:
    cr_idx = get_property_node(snapshot, meta_idx, "content_references", edge_offsets)
    if cr_idx is None:
        return []
    refs: list[dict] = []
    children = _iter_array_like_children(snapshot, cr_idx, edge_offsets)
    if not children:
        return []
    for i, ref_idx in children:
        ref: dict = {"index": i}
        for k in ("name", "attribution", "snippet", "title", "url"):
            sk = get_property_string(snapshot, ref_idx, k, edge_offsets)
            if sk:
                ref[k] = sk
        props = _get_object_string_props(snapshot, ref_idx, edge_offsets)
        for k, v in props.items():
            if k not in ref and v:
                ref[k] = v
        items_idx = get_property_node(snapshot, ref_idx, "items", edge_offsets)
        if items_idx is not None:
            item_pairs = _iter_array_like_children(snapshot, items_idx, edge_offsets)
            items: list[dict] = []
            for j, it_idx in item_pairs:
                it: dict = {"index": j}
                for k in ("attribution", "snippet", "title", "url"):
                    sk = get_property_string(snapshot, it_idx, k, edge_offsets)
                    if sk:
                        it[k] = sk
                ip = _get_object_string_props(snapshot, it_idx, edge_offsets)
                for k in ("attribution", "snippet", "title", "url"):
                    if k in ip and k not in it:
                        it[k] = ip[k]
                if len(it) > 1:
                    items.append(it)
            if items:
                ref["items"] = items
        refs.append(ref)
    return refs


def _extract_safe_urls_list(snapshot: dict, meta_idx: int, edge_offsets: list[int] | None) -> list[str]:
    su_idx = get_property_node(snapshot, meta_idx, "safe_urls", edge_offsets)
    if su_idx is None:
        return []
    node = get_node(snapshot, su_idx)
    if (node.get("type") or "").lower() in ("array", "object"):
        return [x for x in _collect_array_strings(snapshot, su_idx, edge_offsets) if x]
    s = get_property_string(snapshot, meta_idx, "safe_urls", edge_offsets)
    return [s] if s else []


def _extract_search_result_groups_enriched(snapshot: dict, meta_idx: int, edge_offsets: list[int] | None) -> list[dict]:
    """Like get_tool_metadata search groups, but include attribution on each entry."""
    result: list[dict] = []
    srg_idx = get_property_node(snapshot, meta_idx, "search_result_groups", edge_offsets)
    if srg_idx is None:
        return result
    group_pairs = _iter_array_like_children(snapshot, srg_idx, edge_offsets)
    if not group_pairs:
        return result
    for _, group_idx in group_pairs:
        group_node = get_node(snapshot, group_idx)
        if group_node.get("type") not in ("object", "array"):
            continue
        props = _get_object_string_props(snapshot, group_idx, edge_offsets)
        domain = props.get("domain") or _get_property_string_or_name(snapshot, group_idx, "domain", edge_offsets) or ""
        entries_idx = get_property_node(snapshot, group_idx, "entries", edge_offsets)
        entries: list[dict] = []
        if entries_idx is not None:
            for _, ent_idx in _iter_array_like_children(snapshot, entries_idx, edge_offsets):
                ent_props = _get_object_string_props(snapshot, ent_idx, edge_offsets)
                title = ent_props.get("title") or _get_property_string_or_name(snapshot, ent_idx, "title", edge_offsets) or ""
                url = ent_props.get("url") or _get_property_string_or_name(snapshot, ent_idx, "url", edge_offsets) or ""
                snippet = ent_props.get("snippet") or _get_property_string_or_name(snapshot, ent_idx, "snippet", edge_offsets) or ""
                attr = ent_props.get("attribution") or get_property_string(snapshot, ent_idx, "attribution", edge_offsets) or ""
                if not any(
                    ((title or "").strip(), (url or "").strip(), (snippet or "").strip(), (attr or "").strip())
                ):
                    continue
                entries.append(
                    {
                        "title": title.strip(),
                        "url": url.strip(),
                        "snippet": snippet.strip(),
                        "attribution": attr.strip(),
                    }
                )
        if domain or entries:
            result.append({"domain": domain, "entries": entries})
    return result


def extract_conversation_message_extra(
    snapshot: dict, msg_node_index: int, edge_offsets: list[int] | None
) -> dict:
    """Structured fields for conversation report: parts, metadata, code block text."""
    out: dict = {}
    content_idx = get_property_node(snapshot, msg_node_index, "content", edge_offsets)
    content_type = None
    code_block_text = None
    if content_idx is not None:
        content_type = get_property_string(snapshot, content_idx, "content_type", edge_offsets)
        if (content_type or "").strip().lower() == "code":
            t = _extract_content_flat_text(snapshot, content_idx, edge_offsets)
            if t and t.strip():
                code_block_text = t.strip()
        parts_idx = get_property_node(snapshot, content_idx, "parts", edge_offsets)
        if parts_idx is not None:
            parts = _extract_message_parts_detail(snapshot, parts_idx, edge_offsets)
            if parts:
                out["parts"] = parts
    if content_type:
        out["content_type"] = content_type
    if code_block_text:
        out["code_block_text"] = code_block_text

    meta_idx = get_property_node(snapshot, msg_node_index, "metadata", edge_offsets)
    if meta_idx is not None:
        mm: dict = {}
        for key in ("model_slug", "image_gen_title", "resolved_model_slug"):
            v = get_property_string(snapshot, meta_idx, key, edge_offsets)
            if v and v.strip():
                mm[key] = v.strip()
        att = _extract_attachments_list(snapshot, meta_idx, edge_offsets)
        if att:
            mm["attachments"] = att
        cr_idx = get_property_node(snapshot, meta_idx, "content_references", edge_offsets)
        cr = _extract_content_references_list(snapshot, meta_idx, edge_offsets)
        if cr:
            mm["content_references"] = cr
        if cr_idx is not None:
            cr_root_name = get_property_string(snapshot, cr_idx, "name", edge_offsets)
            if cr_root_name and cr_root_name.strip():
                mm["content_references_name"] = cr_root_name.strip()
        su = _extract_safe_urls_list(snapshot, meta_idx, edge_offsets)
        if su:
            mm["safe_urls"] = su
        srg = _extract_search_result_groups_enriched(snapshot, meta_idx, edge_offsets)
        if srg:
            mm["search_result_groups"] = srg
        if mm:
            out["message_metadata"] = mm
    return out


def _prune_empty_extra(extra: dict) -> dict:
    if not extra:
        return {}
    cleaned: dict = {}
    for k, v in extra.items():
        if v is None:
            continue
        if isinstance(v, dict) and not v:
            continue
        if isinstance(v, list) and not v:
            continue
        if isinstance(v, str) and not str(v).strip():
            continue
        cleaned[k] = v
    return cleaned


def _merge_part_dicts(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        if k not in out or out[k] in (None, ""):
            out[k] = v
    return out


def _merge_conversation_extra_dict(a: dict | None, b: dict | None) -> dict | None:
    if not a:
        return b if b else None
    if not b:
        return a if a else None
    out = dict(a)
    for k, v in b.items():
        if k not in out or out[k] in (None, "", [], {}):
            out[k] = v
        elif k == "parts" and isinstance(v, list) and isinstance(out.get("parts"), list):
            op, vp = out["parts"], v
            if len(vp) > len(op):
                out["parts"] = vp
            else:
                merged_parts = []
                for i in range(max(len(op), len(vp))):
                    pa = op[i] if i < len(op) else {}
                    pb = vp[i] if i < len(vp) else {}
                    merged_parts.append(_merge_part_dicts(pa, pb))
                out["parts"] = merged_parts
        elif k == "message_metadata" and isinstance(v, dict) and isinstance(out.get("message_metadata"), dict):
            merged = {**out["message_metadata"], **v}
            out["message_metadata"] = merged
    return out


def _merge_message_records(prev: dict, new: dict) -> dict:
    """Combine two records for the same message id (different heap paths)."""
    out = dict(prev)
    if len((new.get("content") or "").strip()) > len((out.get("content") or "").strip()):
        out["content"] = new["content"]
    for k in ("tool_label_suffix", "tool_metadata"):
        if new.get(k) and not out.get(k):
            out[k] = new[k]
    ncode = (new.get("assistant_code_reasoning") or "").strip()
    ocode = (out.get("assistant_code_reasoning") or "").strip()
    if ncode and (not ocode or len(ncode) > len(ocode)):
        out["assistant_code_reasoning"] = new["assistant_code_reasoning"]
    merged_extra = _merge_conversation_extra_dict(prev.get("conversation_extra"), new.get("conversation_extra"))
    if merged_extra:
        out["conversation_extra"] = merged_extra
    return out


def _parts_have_heap_forensics_value(parts: list) -> bool:
    """True if at least one part has text, image metrics, or part-level metadata (skip index-only noise)."""
    for p in parts:
        if not isinstance(p, dict):
            continue
        s = p.get("string")
        if s is not None and str(s).strip():
            return True
        for k in ("height", "width", "size_bytes"):
            v = p.get(k)
            if v is not None and v != "":
                return True
        md = p.get("metadata")
        if isinstance(md, dict) and md:
            return True
    return False


def _search_result_groups_have_value(groups: list) -> bool:
    for g in groups:
        if not isinstance(g, dict):
            continue
        if str(g.get("domain") or "").strip():
            return True
        for ent in g.get("entries") or []:
            if not isinstance(ent, dict):
                continue
            if any(
                str(ent.get(k) or "").strip()
                for k in ("title", "url", "snippet", "attribution")
            ):
                return True
    return False


def _conversation_extra_has_substantive_fields(extra: dict | None) -> bool:
    """True when extra has heap fields beyond model_slug / resolved_model_slug / content_type alone."""
    if not extra:
        return False
    mm = extra.get("message_metadata") or {}
    parts = extra.get("parts") or []
    if parts and _parts_have_heap_forensics_value(parts):
        return True
    if mm.get("attachments"):
        return True
    if mm.get("content_references"):
        return True
    if mm.get("safe_urls"):
        return True
    if mm.get("content_references_name"):
        return True
    if mm.get("search_result_groups") and _search_result_groups_have_value(mm["search_result_groups"]):
        return True
    if (mm.get("image_gen_title") or "").strip():
        return True
    return False


def _html_conversation_extra(extra: dict | None, include_model_slug_line: bool = True) -> str:
    if not extra:
        return ""
    mm = extra.get("message_metadata") or {}
    parts = extra.get("parts") or []
    blocks: list[str] = []
    if parts and _parts_have_heap_forensics_value(parts):
        lines = []
        for p in parts[:80]:
            bits = [f"part[{p.get('index', '?')}]"]
            for k in ("height", "width", "size_bytes", "string"):
                if k in p and p[k] is not None:
                    bits.append(f"{k}={p[k]}")
            meta = p.get("metadata") or {}
            if meta:
                bits.append("metadata=" + str(meta)[:500])
            lines.append(" · ".join(str(b) for b in bits))
        blocks.append(
            '<details class="msg-extra"><summary>Heap: content.parts (extra fields only)</summary>'
            "<pre class=\"msg-extra-pre\">" + escape_html("\n".join(lines)) + "</pre></details>"
        )
    if include_model_slug_line and (
        mm.get("model_slug") or mm.get("resolved_model_slug") or mm.get("image_gen_title")
    ):
        bits = []
        if mm.get("model_slug"):
            bits.append(f"model_slug: {escape_html(mm['model_slug'])}")
        if mm.get("resolved_model_slug"):
            bits.append(f"resolved_model_slug: {escape_html(mm['resolved_model_slug'])}")
        if mm.get("image_gen_title"):
            bits.append(f"image_gen_title: {escape_html(mm['image_gen_title'])}")
        blocks.append('<div class="msg-extra-line">' + " · ".join(bits) + "</div>")
    if mm.get("attachments"):
        lines = []
        for i, a in enumerate(mm["attachments"][:50]):
            lines.append(str(a))
        blocks.append(
            "<details class=\"msg-extra\"><summary>Attachments</summary><pre class=\"msg-extra-pre\">"
            + escape_html("\n".join(lines))
            + "</pre></details>"
        )
    if mm.get("content_references"):
        lines = []
        for cr in mm["content_references"][:40]:
            lines.append(str(cr))
        blocks.append(
            "<details class=\"msg-extra\"><summary>Content references</summary><pre class=\"msg-extra-pre\">"
            + escape_html("\n".join(lines))
            + "</pre></details>"
        )
    if mm.get("content_references_name"):
        blocks.append(
            '<div class="msg-extra-line">content_references.name: '
            + escape_html(mm["content_references_name"])
            + "</div>"
        )
    if mm.get("safe_urls"):
        blocks.append(
            "<details class=\"msg-extra\"><summary>Safe URLs</summary><pre class=\"msg-extra-pre\">"
            + escape_html("\n".join(mm["safe_urls"][:100]))
            + "</pre></details>"
        )
    if mm.get("search_result_groups") and _search_result_groups_have_value(mm["search_result_groups"]):
        lines = []
        for g in mm["search_result_groups"][:25]:
            dom = g.get("domain") or ""
            lines.append(f"[{dom}]")
            for ent in (g.get("entries") or [])[:20]:
                lines.append(
                    "  "
                    + (ent.get("title") or "")
                    + " | "
                    + (ent.get("url") or "")
                    + " | "
                    + (ent.get("attribution") or "")
                )
        blocks.append(
            "<details class=\"msg-extra\"><summary>Search result groups</summary><pre class=\"msg-extra-pre\">"
            + escape_html("\n".join(lines))
            + "</pre></details>"
        )
    if not blocks:
        return ""
    return '<div class="msg-extra-wrap">' + "".join(blocks) + "</div>"


def _html_assistant_code_reasoning(text: str) -> str:
    return (
        '<div class="msg-code-reasoning">'
        '<div class="msg-code-reasoning-label">Assistant internal reasoning (code)</div>'
        '<pre class="msg-code-reasoning-body">' + escape_html(text) + "</pre></div>"
    )


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

    # Primary: message.content.parts[*].text[*] in index order
    strings_parts = get_all_text_from_message_parts(snapshot, msg_node_index, edge_offsets)

    # Secondary: elements tree (legacy); some formats only keep full text here
    strings_elements: list[str] = []
    if elements_tree:
        strings_elements = collect_strings_from_elements_tree(elements_tree)

    # Concatenate both sources in order; allow mild duplication, normalize in _parse_content_parts.
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
    graph_owner_idx = object_node_index if object_node_index is not None else msg_node_index
    parent_id = get_property_string(snapshot, graph_owner_idx, "parentId", edge_offsets)
    if not (parent_id or "").strip():
        parent_id = get_property_string(snapshot, msg_node_index, "parentId", edge_offsets)
    child_ids = get_children_ids_from_owner(snapshot, graph_owner_idx, edge_offsets)
    if not child_ids:
        child_ids = get_children_ids_from_owner(snapshot, msg_node_index, edge_offsets)

    rec = {
        "id": get_property_string(snapshot, msg_node_index, "id", edge_offsets),
        "parentId": parent_id,
        "children": child_ids,
        "role": role,
        "create_time": get_create_time_value(snapshot, msg_node_index, edge_offsets),
        "channel": get_property_string(snapshot, msg_node_index, "channel", edge_offsets),
        "content": content.strip(),
    }
    if tool_label_suffix is not None:
        rec["tool_label_suffix"] = tool_label_suffix
    if tool_metadata_structured is not None:
        rec["tool_metadata"] = tool_metadata_structured

    conv_extra = _prune_empty_extra(extract_conversation_message_extra(snapshot, msg_node_index, edge_offsets))
    if conv_extra:
        rec["conversation_extra"] = conv_extra
    ct = (conv_extra.get("content_type") or "").strip().lower()
    if ct == "code" and conv_extra.get("code_block_text"):
        rec["assistant_code_reasoning"] = conv_extra["code_block_text"]
    rec["message_node_index"] = msg_node_index
    return rec


def _graph_key(m: dict) -> str:
    """Stable key for graph algorithms when message.id duplicates (e.g. client-created-root)."""
    mid = (m.get("id") or "").strip()
    if mid and mid != CLIENT_CREATED_ROOT:
        return mid
    nix = m.get("message_node_index")
    if nix is not None:
        return f"__msgidx_{nix}"
    return mid or "__unknown__"


def _graph_key_lookup(pid: str, by_id: dict[str, dict]) -> str | None:
    if not pid:
        return None
    if pid in by_id:
        return pid
    for k, m in by_id.items():
        if (m.get("id") or "").strip() == pid:
            return k
    return None


def _group_messages_by_message_graph(messages: list[dict]) -> list[list[dict]]:
    """One conversation thread per connected component on id/parentId/children (child/sibling links keep threads even if a parent is outside the snapshot)."""
    by_id: dict[str, dict] = {}
    for m in messages:
        gk = _graph_key(m)
        if gk:
            by_id[gk] = m
    if not by_id:
        return []
    parent = {i: i for i in by_id}
    rank = {i: 0 for i in by_id}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if rank[ra] < rank[rb]:
            parent[ra] = rb
        elif rank[ra] > rank[rb]:
            parent[rb] = ra
        else:
            parent[rb] = ra
            rank[ra] += 1

    for gk, m in by_id.items():
        pid = (m.get("parentId") or "").strip()
        pk = _graph_key_lookup(pid, by_id)
        if pk:
            union(gk, pk)
        for cid in m.get("children") or []:
            ck = _graph_key_lookup(cid, by_id)
            if ck:
                union(gk, ck)

    buckets: dict[str, list[dict]] = {}
    for gk in by_id:
        r = find(gk)
        buckets.setdefault(r, []).append(by_id[gk])
    return list(buckets.values())


def _stem_id_for_message(graph_key: str, by_id: dict[str, dict]) -> str | None:
    """Stem id reached when walking up to the node whose parentId is CLIENT_CREATED_ROOT; None if the root itself."""
    if graph_key == CLIENT_CREATED_ROOT:
        return None
    seen: set[str] = set()
    cur = graph_key
    while cur and cur in by_id and cur not in seen:
        seen.add(cur)
        pid = (by_id[cur].get("parentId") or "").strip()
        if pid == CLIENT_CREATED_ROOT:
            return cur
        if not pid:
            return None
        parent_k = _graph_key_lookup(pid, by_id)
        if not parent_k:
            return None
        cur = parent_k
    return None


def _group_messages_by_stem_id(messages: list[dict]) -> list[list[dict]]:
    """One thread per stem (direct child of CLIENT_CREATED_ROOT); split stems even when they share the same nominal root id string."""
    by_id: dict[str, dict] = {}
    for m in messages:
        gk = _graph_key(m)
        if gk:
            by_id[gk] = m
    if not by_id:
        return []
    buckets: dict[str, list[dict]] = {}
    for m in messages:
        gk = _graph_key(m)
        if not gk:
            buckets.setdefault("__no_id__", []).append(m)
            continue
        stem = _stem_id_for_message(gk, by_id)
        mid_raw = (m.get("id") or "").strip()
        if stem is not None:
            key = stem
        elif mid_raw == CLIENT_CREATED_ROOT:
            kids = m.get("children") or []
            key = kids[0] if kids else CLIENT_CREATED_ROOT
        else:
            key = f"__unanchored__:{gk}"
        buckets.setdefault(key, []).append(m)
    return list(buckets.values())


def _has_client_created_root_anchor(messages: list[dict]) -> bool:
    """Whether this snapshot can use stem-based splitting (older data falls back to union–find components)."""
    for m in messages:
        if (m.get("parentId") or "").strip() == CLIENT_CREATED_ROOT:
            return True
        if m.get("id") == CLIENT_CREATED_ROOT:
            return True
    return False


def cluster_messages_into_threads(messages: list[dict]) -> list[list[dict]]:
    """Prefer stems (direct under client root); if no anchor, split by id/parentId/children components."""
    if _has_client_created_root_anchor(messages):
        return _group_messages_by_stem_id(messages)
    return _group_messages_by_message_graph(messages)


def collect_unique_message_records_from_entries(
    snapshot: dict,
    uuid_entries: list,
    edge_offsets: list[int] | None,
) -> list[dict]:
    """Collect message records from all entries, deduplicate by heap message node index (not message.id string).

    Multiple wrappers can point at the same message node; message.id may be client-created-root or duplicated.
    """
    by_msg_idx: dict[int, dict] = {}
    for entry in uuid_entries:
        if len(entry) < 5 or entry[4] is None:
            continue
        part_of_key_child = entry[2]
        msg_parts_elements = entry[4]
        path_chain, elements_tree = msg_parts_elements
        if not path_chain:
            continue
        msg_idx = path_chain[0][0]["node_index"]
        obj_idx = part_of_key_child.get("node_index") if part_of_key_child else None
        rec = _build_message_record(snapshot, msg_idx, elements_tree, edge_offsets, object_node_index=obj_idx)
        if msg_idx not in by_msg_idx:
            by_msg_idx[msg_idx] = rec
        else:
            by_msg_idx[msg_idx] = _merge_message_records(by_msg_idx[msg_idx], rec)
    return list(by_msg_idx.values())


def _component_latest_ts(messages: list[dict]) -> float:
    best = 0.0
    for m in messages:
        t = _parse_create_time_to_ts(m.get("create_time"))
        if t is not None and t > best:
            best = t
    return best


def _order_messages_by_parent_chain(messages: list[dict]) -> list[dict]:
    """Order messages using id/parentId/children as a dependency graph.
    - Parents (via parentId or children edges) always precede children.
    - Within a level, sort by create_time (numeric) then id.
    This preserves typical user → assistant → follow-up turn order."""
    by_id: dict[str, dict] = {}
    for m in messages:
        gk = _graph_key(m)
        if gk:
            by_id[gk] = m
    if not by_id:
        return sorted(messages, key=lambda m: (_parse_create_time_to_ts(m.get("create_time")) or float("inf"), m.get("id") or ""))

    children_map: dict[str, set[str]] = {gk: set() for gk in by_id}
    indegree: dict[str, int] = {gk: 0 for gk in by_id}
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
        gk = _graph_key(m)
        if not gk:
            continue
        pid = m.get("parentId")
        if pid:
            pk = _graph_key_lookup(pid, by_id)
            if pk:
                _add_edge(pk, gk)
    for m in messages:
        gk = _graph_key(m)
        if not gk:
            continue
        for cid in (m.get("children") or []):
            if cid:
                ck = _graph_key_lookup(cid, by_id)
                if ck:
                    _add_edge(gk, ck)

    def _key(gk: str) -> tuple[float, str]:
        msg = by_id[gk]
        ts = _parse_create_time_to_ts(msg.get("create_time"))
        return (ts if ts is not None else float("inf"), gk)

    import heapq
    heap: list[tuple[float, str]] = []
    for gk, deg in indegree.items():
        if deg == 0:
            heapq.heappush(heap, _key(gk))

    ordered_ids: list[str] = []
    while heap:
        _, gk = heapq.heappop(heap)
        ordered_ids.append(gk)
        for cid in children_map.get(gk, ()):
            indegree[cid] -= 1
            if indegree[cid] == 0:
                heapq.heappush(heap, _key(cid))

    remaining = [gk for gk in by_id.keys() if gk not in ordered_ids]
    remaining.sort(key=_key)
    ordered_ids.extend(remaining)

    ordered = [by_id[gk] for gk in ordered_ids]

    no_id = [m for m in messages if not _graph_key(m)]
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
        ".msg-code-reasoning { margin-bottom: 10px; padding: 10px 12px; background: #1a2332; border-radius: 8px; border-left: 3px solid #a371f7; }\n"
        ".msg-code-reasoning-label { font-size: 0.7rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; color: #c9d1d9; margin-bottom: 6px; }\n"
        ".msg-code-reasoning-body { margin: 0; white-space: pre-wrap; font-size: 0.875rem; color: #e6edf3; font-family: ui-monospace, Consolas, monospace; }\n"
        ".msg-extra-wrap { margin: 8px 0; font-size: 0.8125rem; }\n"
        ".msg-extra { margin: 6px 0; }\n"
        ".msg-extra summary { cursor: pointer; color: #58a6ff; }\n"
        ".msg-extra-pre { margin: 6px 0 0 0; padding: 8px 10px; background: #161b22; border-radius: 6px; white-space: pre-wrap; word-break: break-word; color: #c9d1d9; font-size: 0.75rem; max-height: 320px; overflow: auto; }\n"
        ".msg-extra-line { margin: 4px 0; color: #8b949e; }\n"
    )


def generate_conversation_html(
    snapshot: dict, uuid_entries: list, edge_offsets: list[int] | None, out_path: str
) -> None:
    """Write conversation_threads.html from globally collected messages (stem threads or graph components; not time-sliced)."""
    all_messages = collect_unique_message_records_from_entries(snapshot, uuid_entries, edge_offsets)
    if not all_messages:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("<!DOCTYPE html><html><body><p>No conversation data (message path not found).</p></body></html>")
        return
    components = cluster_messages_into_threads(all_messages)
    components.sort(key=_component_latest_ts, reverse=True)
    thread_blocks: list[tuple[float, str]] = []
    for comp in components:
        ordered = _order_messages_by_parent_chain(comp)
        if not ordered:
            ordered = comp
        bubbles = []
        for m in ordered:
            content_raw = (m.get("content") or "").strip()
            extra = m.get("conversation_extra") if isinstance(m.get("conversation_extra"), dict) else {}
            code_r = (m.get("assistant_code_reasoning") or "").strip()
            substantive_extra = _conversation_extra_has_substantive_fields(extra)
            include_model_line = bool(content_raw) or bool(code_r) or substantive_extra
            extra_html = _html_conversation_extra(extra, include_model_slug_line=include_model_line)
            if not content_raw and not extra_html and not code_r:
                continue
            body_parts: list[str] = []
            if code_r:
                body_parts.append(_html_assistant_code_reasoning(code_r))
            if extra_html:
                body_parts.append(extra_html)
            if content_raw:
                content = sanitize_message_content(content_raw)
                if (m.get("channel") or "").strip().lower() == "commentary":
                    content += " [System/storage]"
                body_parts.append(f'<div class="content">{content}</div>')
            role_label = "User" if m.get("role") == "user" else ("Tool (" + m.get("tool_label_suffix", "tool") + ")" if m.get("role") == "tool" else "Assistant (ChatGPT)")
            time_display = _format_display_time(m.get("create_time"))
            bubbles.append(
                f'<div class="msg {m.get("role", "unknown")}">'
                f'<div class="role">{role_label}</div>'
                + "".join(body_parts)
                + f'<div class="time">Time: {escape_html(time_display)}</div></div>'
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
            f'<div class="thread-title">Thread{escape_html(time_range) if time_range else ""}</div>'
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
    """Same grouping as HTML: one threads[] element per stem- or graph-based thread."""
    all_messages = collect_unique_message_records_from_entries(snapshot, uuid_entries, edge_offsets)
    if not all_messages:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"threads": [], "message": "No conversation data (message path not found)."}, f, ensure_ascii=False, indent=2)
        return
    components = cluster_messages_into_threads(all_messages)
    components.sort(key=_component_latest_ts, reverse=True)
    thread_list: list[tuple[float, dict]] = []
    for comp in components:
        ordered = _order_messages_by_parent_chain(comp)
        if not ordered:
            ordered = comp
        session_start = None
        session_end = None
        out_messages = []
        latest_ts = 0.0
        for m in ordered:
            t = _parse_create_time_to_ts(m.get("create_time"))
            if t is not None and t > latest_ts:
                latest_ts = t
            content_raw = (m.get("content") or "").strip()
            extra = m.get("conversation_extra") or {}
            code_r = (m.get("assistant_code_reasoning") or "").strip()
            if not content_raw and not code_r and not extra:
                continue
            content_plain, entities = _content_plain_and_entities(content_raw) if content_raw else ("", [])
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
            if code_r:
                msg_obj["assistant_code_reasoning"] = code_r
                msg_obj["assistant_code_reasoning_label"] = "Assistant internal reasoning (code)"
            if extra:
                msg_obj["conversation_extra"] = extra
            if m.get("message_node_index") is not None:
                msg_obj["message_node_index"] = m["message_node_index"]
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
        ".thread-section { margin: 2rem 0; padding: 1rem 1rem 1.25rem 1.25rem; background: #0d1117; border-radius: 8px; border: 1px solid #30363d; }\n"
        ".thread-heading { font-size: 1rem; font-weight: 600; color: #58a6ff; margin: 0 0 1rem 0; }\n"
    )


def _collect_adaptive_candidate_entries(
    snapshot: dict, edge_offsets: list[int] | None
) -> list[tuple]:
    """Find conversation-like objects globally by property signature.

    Candidate signature: object node with id/parentId/children/message.
    This intentionally avoids any fixed path assumptions (e.g. WeakMap/table).
    """
    node_count = snapshot["snapshot"]["node_count"]
    entries: list[tuple] = []
    for node_index in range(node_count):
        node = get_node(snapshot, node_index)
        if node.get("type") != "object":
            continue
        if not _node_has_required_props(snapshot, node_index, edge_offsets):
            continue

        msg_parts_elements = get_message_content_parts_elements_tree(snapshot, node_index, edge_offsets)
        if msg_parts_elements is None:
            msg_idx = get_property_node(snapshot, node_index, "message", edge_offsets)
            if msg_idx is not None:
                msg_node = get_node(snapshot, msg_idx)
                msg_node["edge_from_parent"] = {"type": "property", "label": "message"}
                msg_parts_elements = ([(msg_node, msg_node["edge_from_parent"])], {})

        id_str = get_property_string(snapshot, node_index, "id", edge_offsets)
        # Keep legacy entry shape so downstream conversation HTML/JSON can reuse current logic.
        entries.append((node, node, node, None, msg_parts_elements, id_str))
    return entries


def generate_html_weakmaps(
    snapshot: dict,
    uuid_only_path: str,
    write_structure_report: bool = True,
    write_conversation: bool = True,
) -> tuple[int, int]:
    """Generate reports using adaptive global signature scan (no fixed container path dependency).

    Returns (uuid_entries_count, message_path_count).
    """
    edge_offsets = get_edge_offsets(snapshot)
    uuid_entries = _collect_adaptive_candidate_entries(snapshot, edge_offsets)
    found_message_path = sum(1 for e in uuid_entries if len(e) > 4 and e[4] is not None)

    out_dir = os.path.dirname(uuid_only_path)
    if write_conversation:
        conversation_path = os.path.join(out_dir, "conversation_threads.html")
        conversation_json_path = os.path.join(out_dir, "conversation_threads.json")
        generate_conversation_html(snapshot, uuid_entries, edge_offsets, conversation_path)
        generate_conversation_json(snapshot, uuid_entries, edge_offsets, conversation_json_path)

    if not write_structure_report:
        return len(uuid_entries), found_message_path

    partition: tuple[list[int], list[list], list, list[list[dict]]] | None = None
    if uuid_entries:
        partition = _partition_structure_report_entries(snapshot, uuid_entries, edge_offsets)

    header_uuid = (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"UTF-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">\n"
        "<title>Structure Report — Heap Snapshot Forensics</title>\n<style>\n"
        + _common_css()
        + "</style>\n</head>\n<body>\n"
        "<header class=\"report-header\"><h1>Structure Report</h1></header>\n"
    )
    with open(uuid_only_path, "w", encoding="utf-8") as f:
        f.write(header_uuid)
        if not uuid_entries:
            f.write("<p>No conversation-like object found by adaptive signature scan.</p>")
            f.write("</body></html>")
            return 0, 0

        thread_order, thread_entries, orphan_entries, components = partition

        def write_one_structure_entry(entry: tuple) -> None:
            obj_node = entry[2]
            msg_parts_elements = entry[4] if len(entry) > 4 else None
            id_str = entry[5] if len(entry) > 5 else None
            obj_idx = obj_node["node_index"]
            f.write("<details>\n<summary class=\"node\">")
            f.write(object_summary_html(snapshot, obj_node, None, edge_offsets, fallback_uuid=id_str))
            f.write("</summary>\n")
            write_structure_report_wrapper_subtree(f, snapshot, obj_idx, edge_offsets)
            ext = _structure_entry_extracted_text(snapshot, entry, edge_offsets)
            if msg_parts_elements is not None:
                f.write('<ul class="tree"><li class="node node-message-text"><span class="meta\">Extracted text: </span>')
                f.write(escape_html(ext or "(no text)"))
                f.write("</li></ul>")
            else:
                f.write('<ul class="tree"><li class="node node-missing">Message path not found for this object.</li></ul>')
            f.write("</details>\n")

        for ti in thread_order:
            ents = thread_entries[ti]
            if not ents:
                continue
            title = escape_html(_structure_thread_section_title(components[ti]))
            f.write('<section class="thread-section">')
            f.write(f'<h2 class="thread-heading">Thread — {title}</h2>\n')
            for ent in ents:
                write_one_structure_entry(ent)
            f.write("</section>\n")

        if orphan_entries:
            f.write('<section class="thread-section">')
            f.write('<h2 class="thread-heading">Other (unassigned to thread)</h2>\n')
            for ent in orphan_entries:
                write_one_structure_entry(ent)
            f.write("</section>\n")
        f.write("</body></html>")
    return len(uuid_entries), found_message_path


def _format_byte_size(n: int) -> str:
    """Return a human-readable byte size (binary units)."""
    if n < 1024:
        return f"{n} B"
    v = float(n)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        v /= 1024.0
        if v < 1024:
            return f"{v:.2f} {unit}"
    return f"{v:.2f} PiB"


def _hash_file_md5_sha256(path: str, chunk_size: int = 1024 * 1024) -> tuple[str, str]:
    """Compute MD5 and SHA-256 in one pass (for chain-of-custody style records)."""
    md5 = hashlib.md5()
    sha256 = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            md5.update(chunk)
            sha256.update(chunk)
    return md5.hexdigest(), sha256.hexdigest()


def _snapshot_model_stat_lines(snapshot: dict | None) -> list[str]:
    if not snapshot:
        return ["(Snapshot not loaded; node/edge counts unavailable.)"]
    sn = snapshot.get("snapshot") or {}
    lines: list[str] = []
    nc = sn.get("node_count")
    if nc is not None:
        lines.append(f"snapshot.node_count: {nc}")
    edges = sn.get("edges")
    if isinstance(edges, list):
        lines.append(f"snapshot.edges (array length): {len(edges)}")
    else:
        ec = sn.get("edge_count")
        if ec is not None:
            lines.append(f"snapshot.edge_count: {ec}")
    return lines or ["(No node_count or edges metadata in snapshot payload.)"]


def write_forensic_run_summary(
    path: str,
    *,
    tool_version: str,
    snapshot_path: str,
    output_dir: str,
    analysis_start_utc_iso: str,
    elapsed_sec: float,
    result: dict,
    snapshot: dict | None,
    generate_structure_report: bool,
) -> None:
    """Write a plain-text forensic-style run record next to HTML/JSON outputs."""
    lines: list[str] = []
    ap = os.path.abspath(snapshot_path)
    od = os.path.abspath(output_dir)

    lines.append("=" * 78)
    lines.append("V8 HEAP SNAPSHOT FORENSICS — RUN RECORD")
    lines.append("=" * 78)
    lines.append("")
    lines.append(f"Tool version: {tool_version}")
    lines.append(f"Python: {sys.version.split()[0]} ({sys.executable})")
    lines.append(f"Platform: {platform.platform()}")
    lines.append(f"Processor: {platform.processor() or 'n/a'}")
    lines.append(f"Hostname: {platform.node()}")
    lines.append("")
    lines.append("--- Timing ---")
    lines.append(f"Analysis start (UTC): {analysis_start_utc_iso}")
    lines.append(f"Elapsed wall clock: {elapsed_sec:.6f} s")
    lines.append("")
    lines.append("--- Source file (evidence) ---")
    lines.append(f"Path (absolute): {ap}")
    try:
        st = os.stat(snapshot_path)
        lines.append(f"Size (bytes): {st.st_size}")
        lines.append(f"Size (human): {_format_byte_size(st.st_size)}")
        lines.append(f"Mtime (UTC): {datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()}")
        md5_hex, sha256_hex = _hash_file_md5_sha256(snapshot_path)
        lines.append(f"MD5:    {md5_hex}")
        lines.append(f"SHA-256: {sha256_hex}")
    except OSError as exc:
        lines.append(f"(Could not stat or hash file: {exc})")
    lines.append("")
    lines.append("--- Snapshot parse model ---")
    lines.extend(_snapshot_model_stat_lines(snapshot))
    lines.append("")
    lines.append("--- Extraction summary ---")
    err = result.get("error")
    lines.append(f"Status: {'FAILED' if err else 'SUCCESS'}")
    if err:
        lines.append(f"Error: {err}")
    lines.append("")
    lines.append("--- Output directory ---")
    lines.append(od)
    lines.append("")
    lines.append("--- Output artifacts (this run) ---")
    if generate_structure_report:
        lines.append(f"- structure_report.html: {result.get('uuid_only_path')}")
    else:
        lines.append("- structure_report.html: (not generated; on-demand in GUI)")
    lines.append(f"- conversation_threads.html: {result.get('conversation_path')}")
    lines.append(f"- conversation_threads.json: {result.get('conversation_json_path')}")
    lines.append(f"- {FORENSIC_RUN_SUMMARY_FILENAME}: {os.path.abspath(path)}")
    lines.append("")
    lines.append("=" * 78)
    lines.append("END OF RECORD")
    lines.append("=" * 78)

    out_dir = os.path.dirname(os.path.abspath(path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def run_analysis(
    snapshot_path: str,
    output_dir: str | None = None,
    generate_structure_report: bool = True,
) -> dict:
    """
    Load a heap snapshot, run WeakMap/UUID and conversation extraction, write HTML reports.
    When generate_structure_report is False, only conversation_threads are written (structure_report on demand).
    If output_dir is omitted, writes next to heap_forensics.py (the tool directory).
    Returns dict with keys: uuid_only_path, conversation_path, conversation_json_path,
    forensic_summary_path, snapshot_path, error (if any).
    """
    if output_dir is None:
        output_dir = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(output_dir, exist_ok=True)
    uuid_only_path = os.path.join(output_dir, "structure_report.html")
    conversation_path = os.path.join(output_dir, "conversation_threads.html")
    conversation_json_path = os.path.join(output_dir, "conversation_threads.json")
    forensic_summary_path = os.path.join(output_dir, FORENSIC_RUN_SUMMARY_FILENAME)
    result = {
        "snapshot_path": snapshot_path,
        "uuid_only_path": uuid_only_path,
        "conversation_path": conversation_path,
        "conversation_json_path": conversation_json_path,
        "forensic_summary_path": forensic_summary_path,
        "error": None,
        "weakmap_count": 0,
        "uuid_entries_count": 0,
        "message_path_count": 0,
    }
    t0 = time.perf_counter()
    analysis_start = datetime.now(timezone.utc).isoformat()
    snapshot: dict | None = None
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
        # Backward-compatible field name. Now means "adaptive candidate count".
        result["weakmap_count"] = num_entries
    except Exception as e:
        result["error"] = str(e)
    finally:
        elapsed = time.perf_counter() - t0
        try:
            write_forensic_run_summary(
                forensic_summary_path,
                tool_version=TOOL_VERSION,
                snapshot_path=snapshot_path,
                output_dir=output_dir,
                analysis_start_utc_iso=analysis_start,
                elapsed_sec=elapsed,
                result=result,
                snapshot=snapshot,
                generate_structure_report=generate_structure_report,
            )
        except Exception:
            pass
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
    print("Forensic summary:", result.get("forensic_summary_path", ""))
    print("(See HTML header for message->content->parts->elements count; 0 = path not in heap or different names)")


if __name__ == "__main__":
    main()
