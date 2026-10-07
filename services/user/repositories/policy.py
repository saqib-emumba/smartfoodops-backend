"""PostgreSQL access for the access-control policy tables (D57).

Separate from repositories/users.py because these three tables answer a different question
from the rest of this service: not "who is this account" but "what may a caller holding
these roles do". They are read as a whole — the policy decision point loads both queries
into one snapshot and matches against it in memory — rather than queried per request, so
nothing here takes a user id or a path.

Both reads are deliberately unfiltered full-table scans. The tables are reference data of a
few dozen rows, and loading everything once per TTL is what keeps a database round trip out
of the request path that every gated call in the platform now goes through.
"""

from common.repository import Repository

# LEFT JOIN, not JOIN: a NULL permission_id is the meaningful "any authenticated caller"
# case, and an inner join would silently drop exactly those rows — turning nine
# ownership-gated routes into unmatched paths, which fail closed. The whole platform's
# profile reads would 403.
_SELECT_ROUTE_PERMISSIONS = """
    SELECT rp.method, rp.path_pattern, p.name AS permission
    FROM route_permissions rp
    LEFT JOIN permissions p ON p.id = rp.permission_id
"""

# array_agg per role, the same shape repositories/users.py uses for role names, so the
# caller gets one row per role rather than a cross product it has to fold itself.
_SELECT_ROLE_PERMISSIONS = """
    SELECT r.name AS role, array_agg(p.name ORDER BY p.name) AS permissions
    FROM roles r
    JOIN role_permissions rp ON rp.role_id = r.id
    JOIN permissions p ON p.id = rp.permission_id
    GROUP BY r.name
"""


class PolicyRepository(Repository):
    def load_routes(self) -> list[dict]:
        """Every row of `route_permissions`, with the permission name resolved.

        `permission` is None for a route any authenticated caller may reach.
        """
        return self.all(_SELECT_ROUTE_PERMISSIONS)

    def load_role_permissions(self) -> dict[str, frozenset[str]]:
        """The grant matrix, as `{role_name: {permission, ...}}`.

        A role holding no permissions is absent rather than present-and-empty. Callers look
        roles up with a default, so the two are equivalent, and the query says what it means.
        """
        return {
            row["role"]: frozenset(row["permissions"])
            for row in self.all(_SELECT_ROLE_PERMISSIONS)
        }
