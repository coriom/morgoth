"""Print the management app's actual OpenAPI without files, auth, or startup."""
from __future__ import annotations

import json
from pathlib import Path
import secrets

from core.runtime import RuntimeHome
from api.management_app import ManagementSecurity, create_management_app
from project_manager import ProjectManager


def main() -> None:
    """Generate schema from the actual app factory with a synthetic token."""
    home_path = Path("/tmp/morgoth-management-openapi")
    home = RuntimeHome(home_path, home_path / "projects")
    app = create_management_app(ProjectManager(home),
                                ManagementSecurity(token=secrets.token_urlsafe(32), port=8765))
    print(json.dumps(app.openapi(), sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
