"""Explicit owner-initiated creation of a private integration repository."""

import re

import httpx

from decagent.integrations.github import API_VERSION, GitHubError


def create_private_test_repository(token, name, *, client=None):
    """Call only from authorized setup, never expose this function as an agent tool."""
    if not token or not re.fullmatch(r"decagent-integration-test-[a-z0-9-]{1,40}", name):
        raise ValueError("A token and dedicated integration repository name are required")
    owned = client is None
    connection = client or httpx.Client(timeout=20, follow_redirects=False)
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
    }
    description = "Private sandbox for DecAgent integration verification."
    try:
        response = connection.get("https://api.github.com/user", headers=headers)
        if response.status_code != 200:
            raise GitHubError("GitHub owner authentication failed", status=response.status_code)
        account = response.json()
        login = account.get("login")
        if not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", login):
            raise GitHubError("Invalid authenticated GitHub owner identity")
        endpoint = f"https://api.github.com/repos/{login}/{name}"
        previous = connection.get(endpoint, headers=headers)
        if previous.status_code == 200:
            repository = previous.json()
            if (
                repository.get("description") != description
                or repository.get("private") is not True
            ):
                raise GitHubError("Repository name exists with different scope; preserve it")
        elif previous.status_code == 404:
            try:
                created = connection.post(
                    "https://api.github.com/user/repos",
                    headers=headers,
                    json={
                        "name": name,
                        "description": description,
                        "private": True,
                        "auto_init": True,
                        "has_projects": False,
                        "has_wiki": False,
                        "allow_auto_merge": False,
                    },
                )
            except httpx.TransportError as exc:
                # One bounded read after a lost response; never blindly repeat creation.
                observed = connection.get(endpoint, headers=headers)
                if observed.status_code != 200:
                    raise GitHubError(
                        "Repository creation outcome is unknown", unknown=True
                    ) from exc
                created = observed
            if created.status_code not in {200, 201}:
                raise GitHubError(
                    "GitHub repository-creation permission is required", status=created.status_code
                )
            repository = created.json()
        else:
            raise GitHubError("Cannot reconcile test repository", status=previous.status_code)
        if (
            repository.get("full_name", "").casefold() != f"{login}/{name}".casefold()
            or repository.get("private") is not True
            or repository.get("description") != description
        ):
            raise GitHubError("Created repository identity differs from setup intent")
        return {
            "repository": repository["full_name"],
            "repository_id": repository["id"],
            "url": repository["html_url"],
            "private": True,
            "default_branch": repository["default_branch"],
            "owner_id": account["id"],
            "owner_login": login,
        }
    except httpx.HTTPError as exc:
        raise GitHubError("GitHub setup connection failed") from exc
    finally:
        if owned:
            connection.close()
