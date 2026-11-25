"""Project data products and observed downstream impact by column."""

from collections import deque
import time
import uuid
from urllib.parse import urlsplit

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from datalog.auth.access import AccessError, audit
from datalog.data.data_lineage import array_field, asset, describe, label, object_field
from datalog.core.validation import Invalid


class DataProducts:
    def __init__(self, connect):
        self.connect = connect

    def save(self, project, actor, body):
        name = label(body.get("name"), "product", 120)
        raw = array_field(body.get("members"), "ativos", 80)
        members = {}
        for item in raw:
            item = object_field(item, "asset")
            ident = label(item.get("asset_id"), "asset", 32)
            role = item.get("role")
            if ident in members or role not in ("table", "consumer"):
                raise Invalid("Use distinct assets with role table or consumer")
            members[ident] = role
        if "table" not in members.values():
            raise Invalid("Select at least one eligible table")
        try:
            with self.connect() as db:
                for ident in sorted(members):
                    asset(db, project, ident)
                if body.get("id"):
                    product = label(body["id"], "product", 32)
                    changed = db.execute(
                        "UPDATE data_products SET name=%s WHERE project=%s AND id=%s", (name, project, product)
                    ).rowcount
                    if not changed:
                        raise AccessError(404, "Product not found in this project")
                    db.execute(
                        "DELETE FROM data_product_members WHERE project=%s AND product_id=%s", (project, product)
                    )
                else:
                    product = uuid.uuid4().hex
                    db.execute("INSERT INTO data_products VALUES(%s,%s,%s)", (project, product, name))
                db.cursor().executemany(
                    "INSERT INTO data_product_members VALUES(%s,%s,%s,%s)",
                    [(project, product, ident, role) for ident, role in members.items()],
                )
                audit(db, project, actor, "data.product.save", product)
        except UniqueViolation as error:
            raise AccessError(409, "A product with this name already exists") from error
        return {"id": product, "name": name}

    def list(self, project, params):
        ident = params.get("id")
        with self.connect() as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            products = db.execute(
                """SELECT id,name FROM data_products WHERE project=%s
                AND (%s::text IS NULL OR id=%s) ORDER BY name,id LIMIT 100""",
                (project, ident, ident),
            ).fetchall()
            if not products:
                if ident:
                    raise AccessError(404, "Product not found in this project")
                return {"items": []}
            by_id = {row["id"]: {**row, "members": []} for row in products}
            rows = db.execute(
                """SELECT m.product_id,m.asset_id,m.role,a.name,b.url,
                    o.state,o.measured_at,EXISTS (SELECT 1 FROM monitors q
                      WHERE q.project=m.project AND q.configuration @>
                        jsonb_build_object('source','data_quality','asset_id',m.asset_id)) monitored
                FROM data_product_members m JOIN data_assets a ON (m.project,m.asset_id)=(a.project,a.id)
                LEFT JOIN data_bi_reports b ON (b.project,b.asset_id)=(a.project,a.id)
                LEFT JOIN LATERAL (SELECT state,measured_at FROM data_observations o
                    WHERE o.project=m.project AND o.asset_id=m.asset_id ORDER BY measured_at DESC,id LIMIT 1) o ON true
                WHERE m.project=%s AND m.product_id=ANY(%s) ORDER BY a.name,m.asset_id""",
                (project, list(by_id)),
            ).fetchall()
        for row in rows:
            by_id[row.pop("product_id")]["members"].append(row)
        for product in by_id.values():
            tables = [row for row in product["members"] if row["role"] == "table"]
            monitored = sum(row["monitored"] for row in tables)
            healthy = sum(row["monitored"] and row["state"] == "ok" for row in tables)
            alert = any(row["monitored"] and row["state"] in ("alert", "error") for row in tables)
            product.update(
                eligible=len(tables),
                monitored=monitored,
                healthy=healthy,
                state="alert" if alert else "ok" if tables and healthy == len(tables) else "incomplete",
            )
        return {"items": list(by_id.values())}

    def register_report(self, project, actor, body):
        ident = label(body.get("id"), "asset", 32)
        url = label(body.get("url"), "BI URL", 1000)
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or "\\" in url
            or any(ch.isspace() for ch in url)
        ):
            raise Invalid("Use an HTTPS report URL without credentials or parameters")
        with self.connect() as db:
            asset(db, project, ident)
            db.execute(
                """INSERT INTO data_bi_reports VALUES(%s,%s,%s)
                ON CONFLICT(project,asset_id) DO UPDATE SET url=excluded.url""",
                (project, ident, url),
            )
            audit(db, project, actor, "data.report.link", ident)
        return {"id": ident, "url": url}

    def impact(self, project, params):
        column = label(params.get("column"), "column", 200)
        now = time.time()
        with self.connect() as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            db.execute("SET LOCAL statement_timeout = '5s'")
            origin = describe(asset(db, project, params.get("id")))
            queue = deque([(origin["id"], column, 0)])
            visited = set()
            edges, reports, truncated = [], {}, False
            while queue and len(visited) < 80 and len(edges) < 150:
                ident, field, depth = queue.popleft()
                if (ident, field) in visited:
                    continue
                visited.add((ident, field))
                if depth >= 8:
                    truncated = True
                    continue
                rows = db.execute(
                    """SELECT o.asset_id,a.name,a.namespace,b.url,r.id run_id,r.name job,
                        c.edge->>'output_field' output_field
                    FROM data_job_runs r JOIN data_job_outputs o ON (r.project,r.id)=(o.project,o.run_id)
                    JOIN data_assets a ON (o.project,o.asset_id)=(a.project,a.id)
                    LEFT JOIN data_bi_reports b ON (o.project,o.asset_id)=(b.project,b.asset_id)
                    CROSS JOIN LATERAL jsonb_array_elements(r.details->'columns') c(edge)
                    WHERE r.project=%s AND r.state='COMPLETE' AND r.details->'inputs' @> %s
                      AND c.edge->>'input_id'=%s AND c.edge->>'input_field'=%s
                      AND c.edge->>'output_id'=o.asset_id AND o.completed_at<=%s
                      AND NOT EXISTS (SELECT 1 FROM data_job_outputs n WHERE n.project=o.project AND n.asset_id=o.asset_id
                        AND n.completed_at<=%s AND (n.completed_at>o.completed_at
                             OR (n.completed_at=o.completed_at AND n.run_id<o.run_id)))
                    ORDER BY a.namespace,a.name,o.asset_id,output_field,r.id LIMIT 41""",
                    (project, Jsonb([{"id": ident}]), ident, field, now, now),
                ).fetchall()
                room = min(40, 150 - len(edges))
                truncated |= len(rows) > room
                for row in rows[:room]:
                    edges.append({**row, "input_id": ident, "input_field": field, "hop": depth + 1})
                    if row["url"]:
                        report = reports.setdefault(
                            row["asset_id"],
                            {"id": row["asset_id"], "name": row["name"], "url": row["url"], "fields": set()},
                        )
                        report["fields"].add(row["output_field"])
                    queue.append((row["asset_id"], row["output_field"], depth + 1))
        for report in reports.values():
            report["fields"] = sorted(report["fields"])
        return {
            "origin": origin,
            "column": column,
            "edges": edges,
            "reports": list(reports.values()),
            "truncated": truncated or bool(queue),
        }
