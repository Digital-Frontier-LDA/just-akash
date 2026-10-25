"""Fixed org reads and exact acknowledged-ID DELETE; no labels select a target."""

from __future__ import annotations

import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, cast

from .github_jit import _json
from .keydb_qualification_cleanup_native import NoRedirect, number, obj, require

ORG = "/orgs/Borduas-Holdings"


class RunnerAPI:
    def __init__(self, token: str):
        require(
            type(token) is str
            and 0 < len(token) <= 4096
            and all(33 <= ord(c) < 127 for c in token)
        )
        self.token = token
        self.calls = 0
        self.remove_attempted = False

    def request(
        self, method: str, route: str, before_send=None
    ) -> tuple[int, dict[str, Any] | None]:
        read = re.fullmatch(
            re.escape(ORG)
            + (
                r"(?:|/actions/runners(?:\?per_page=100&page=(?:[1-9]|10)"
                r"|/[1-9][0-9]{0,18})|/actions/runner-groups/[1-9][0-9]{0,18}"
                r"(?:/(?:repositories\?per_page=100&page=[12]"
                r"|runners\?per_page=100&page=(?:[1-9]|10)))?)"
            ),
            route,
        )
        require(
            (method == "GET" and read is not None)
            or (
                method == "DELETE"
                and re.fullmatch(re.escape(ORG) + r"/actions/runners/[1-9][0-9]{0,18}", route)
                is not None
                and callable(before_send)
            )
        )
        self.calls += 1
        require(self.calls <= 160)
        if method == "DELETE":
            require(not self.remove_attempted)
            self.remove_attempted = True
            cast(Callable[[], None], before_send)()
        result = None
        try:
            request = urllib.request.Request(
                "https://api.github.com" + route,
                method=method,
                headers={
                    "Authorization": "Bearer " + self.token,
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(
                request, timeout=10
            ) as response:
                status = response.status
                if method == "DELETE":
                    require(status == 204)
                    result = (204, None)
                else:
                    require(status == 200)
                    raw = response.read(1048577)
                    require(len(raw) <= 1048576)
                    result = (200, obj(_json(raw)))
        except urllib.error.HTTPError as error:
            if (
                method == "GET"
                and error.code == 404
                and re.fullmatch(re.escape(ORG) + r"/actions/runners/[1-9][0-9]{0,18}", route)
            ):
                result = (404, None)
        except Exception:
            result = None
        require(result is not None)
        return cast(tuple[int, dict[str, Any] | None], result)

    def document(self, route: str) -> dict[str, Any]:
        status, value = self.request("GET", route)
        require(status == 200)
        return obj(value)

    def runner(self, identity: int) -> dict[str, Any] | None:
        status, value = self.request("GET", ORG + f"/actions/runners/{number(identity)}")
        if status == 404:
            return None
        row = obj(value)
        require(
            number(row.get("id")) == identity
            and type(row.get("busy")) is bool
            and row.get("status") in ("online", "offline")
            and type(row.get("name")) is str
            and bool(row["name"])
        )
        return row

    def population(self, route: str) -> dict[int, dict[str, Any]]:
        result = {}
        total = None
        for page in range(1, 11):
            doc = self.document(route + str(page))
            count = doc.get("total_count")
            rows = doc.get("runners")
            require(
                type(count) is int
                and 0 <= count <= 1000
                and (total is None or total == count)
                and type(rows) is list
                and len(rows) <= 100
            )
            total = count
            for row in cast(list, rows):
                row = obj(row)
                identity = number(row.get("id"))
                require(identity not in result)
                result[identity] = row
            if len(result) == total:
                break
            require(len(cast(list, rows)) == 100 and len(result) < cast(int, total))
        require(len(result) == total)
        return result

    def not_busy(self, identity: int, group_id: int) -> None:
        row = self.runner(identity)
        if row is None:
            require(self.absent(identity))
            return
        require(row["busy"] is False)
        # A rename does not change the acknowledged native registration ID.
        from .keydb_qualification_jit import verify_group

        def read(method, path, token):
            require(method == "GET" and token == self.token)
            return self.document(path)

        verify_group(group_id, read, self.token)
        members = self.population(
            ORG + f"/actions/runner-groups/{number(group_id)}/runners?per_page=100&page="
        )
        require(identity in members and members[identity].get("busy") is False)
        again = self.runner(identity)
        require(again is not None and again["busy"] is False)

    def absent(self, identity: int) -> bool:
        start = time.monotonic()
        organization = self.document(ORG)
        require(
            number(organization.get("id")) == 208689568
            and organization.get("login") == "Borduas-Holdings"
        )
        snapshots = []
        for _ in range(2):
            population = self.population(ORG + "/actions/runners?per_page=100&page=")
            missing = self.runner(identity) is None
            snapshots.append((identity not in population, missing))
        require(time.monotonic() - start < 30)
        return all(listed and direct for listed, direct in snapshots)

    def remove(self, identity: int, before_send) -> None:
        status, _ = self.request(
            "DELETE", ORG + f"/actions/runners/{number(identity)}", before_send
        )
        require(status == 204)
