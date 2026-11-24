"""Bounded dashboard frames and a rows-to-fields transform."""

from datalog.data_lineage import label, object_field
from datalog.validation import Invalid

MAPPING = ("name", "value", "unit", "min", "max", "threshold")


def mapping(config):
    config = object_field(config, "mapeamento")
    if config.keys() - set(MAPPING) or not {"name", "value"} <= config.keys():
        raise Invalid("Map name and value and, optionally, unit and limits")
    return {key: label(config[key], key, 63) for key in MAPPING if key in config}


def frames(columns, rows, fields, truncated):
    projected = []
    seen = set()
    for row in rows:
        name = label(row[fields["name"]], "field name", 160)
        if name in seen:
            raise Invalid("The panel requires unique field names")
        seen.add(name)
        item = {"name": name, **{key: row[column] for key, column in fields.items() if key != "name"}}
        for key in ("value", "min", "max", "threshold"):
            if item.get(key) is not None and type(item[key]) not in (int, float):
                raise Invalid("Value and limits must be numeric")
        if item.get("min") is not None and item.get("max") is not None and item["min"] >= item["max"]:
            raise Invalid("The maximum limit must exceed the minimum")
        projected.append(item)
    return {
        "raw": {"columns": columns, "rows": [[row[column] for column in columns] for row in rows]},
        "transformed": {
            "columns": [item["name"] for item in projected],
            "rows": [[item["value"] for item in projected]] if projected else [],
            "fields": projected,
        },
        "truncated": truncated,
    }
