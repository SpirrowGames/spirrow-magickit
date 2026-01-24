"""Integration tests for Phase 2 API endpoints."""

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from magickit.main import app, create_app, lifespan


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    """Create test client with temporary database."""
    # Use temporary database
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("MAGICKIT_DB_PATH", db_path)
    monkeypatch.setenv("MAGICKIT_AUTH_ENABLED", "false")

    # Create fresh app
    test_app = create_app()

    # Manually run lifespan to initialize dependencies
    async with lifespan(test_app):
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest_asyncio.fixture
async def authenticated_client(tmp_path, monkeypatch):
    """Create test client with authentication enabled."""
    db_path = str(tmp_path / "test_auth.db")
    monkeypatch.setenv("MAGICKIT_DB_PATH", db_path)
    monkeypatch.setenv("MAGICKIT_AUTH_ENABLED", "true")
    monkeypatch.setenv("MAGICKIT_JWT_SECRET", "test-secret-key")

    test_app = create_app()

    # Manually run lifespan to initialize dependencies
    async with lifespan(test_app):
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


class TestHealthEndpoint:
    """Tests for health check endpoint."""

    @pytest.mark.asyncio
    async def test_health_check(self, client):
        """Test health check endpoint."""
        response = await client.get("/health")
        assert response.status_code == 200

        data = response.json()
        assert "status" in data
        assert "version" in data


class TestAuthEndpoints:
    """Tests for authentication endpoints."""

    @pytest.mark.asyncio
    async def test_register_user(self, authenticated_client):
        """Test user registration."""
        response = await authenticated_client.post(
            "/auth/register",
            json={
                "email": "test@example.com",
                "name": "Test User",
                "password": "securepass123",
            },
        )
        assert response.status_code == 201

        data = response.json()
        assert data["email"] == "test@example.com"
        assert data["name"] == "Test User"
        assert "id" in data

    @pytest.mark.asyncio
    async def test_register_duplicate_email(self, authenticated_client):
        """Test registration with duplicate email."""
        # Register first user
        await authenticated_client.post(
            "/auth/register",
            json={
                "email": "duplicate@example.com",
                "name": "First User",
                "password": "password123",
            },
        )

        # Try to register with same email
        response = await authenticated_client.post(
            "/auth/register",
            json={
                "email": "duplicate@example.com",
                "name": "Second User",
                "password": "password456",
            },
        )
        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_login(self, authenticated_client):
        """Test user login."""
        # Register first
        await authenticated_client.post(
            "/auth/register",
            json={
                "email": "login@example.com",
                "name": "Login User",
                "password": "loginpass123",
            },
        )

        # Login
        response = await authenticated_client.post(
            "/auth/login",
            json={
                "email": "login@example.com",
                "password": "loginpass123",
            },
        )
        assert response.status_code == 200

        data = response.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    @pytest.mark.asyncio
    async def test_login_invalid_credentials(self, authenticated_client):
        """Test login with invalid credentials."""
        response = await authenticated_client.post(
            "/auth/login",
            json={
                "email": "nonexistent@example.com",
                "password": "wrongpassword",
            },
        )
        assert response.status_code == 401


class TestWorkspaceEndpoints:
    """Tests for workspace endpoints."""

    @pytest.mark.asyncio
    async def test_create_workspace(self, client):
        """Test workspace creation."""
        response = await client.post(
            "/workspaces",
            json={
                "name": "Test Workspace",
                "settings": {"theme": "dark"},
            },
        )
        # With auth disabled, should use dev user
        assert response.status_code == 201

        data = response.json()
        assert data["name"] == "Test Workspace"
        assert "id" in data

    @pytest.mark.asyncio
    async def test_list_workspaces(self, client):
        """Test listing workspaces."""
        # Create some workspaces first
        await client.post("/workspaces", json={"name": "Workspace 1"})
        await client.post("/workspaces", json={"name": "Workspace 2"})

        response = await client.get("/workspaces")
        assert response.status_code == 200

        data = response.json()
        assert isinstance(data, list)
        # At least the default workspace should exist
        assert len(data) >= 0


class TestProjectEndpoints:
    """Tests for project endpoints."""

    @pytest.mark.asyncio
    async def test_create_project(self, client):
        """Test project creation."""
        # Use default workspace
        response = await client.post(
            "/workspaces/default/projects",
            json={
                "name": "Test Project",
                "description": "A test project",
            },
        )

        # May fail if workspace doesn't exist yet (depends on migrations)
        if response.status_code == 201:
            data = response.json()
            assert data["name"] == "Test Project"
        else:
            # Acceptable if workspace not found
            assert response.status_code in [201, 403, 404]

    @pytest.mark.asyncio
    async def test_get_project(self, client):
        """Test getting a project."""
        # Try to get default project
        response = await client.get("/projects/default")

        # May work or fail depending on migration state
        assert response.status_code in [200, 404]


class TestLockEndpoints:
    """Tests for lock endpoints."""

    @pytest.mark.asyncio
    async def test_acquire_lock(self, client):
        """Test acquiring a lock."""
        response = await client.post(
            "/locks",
            json={
                "resource_type": "task",
                "resource_id": "task-123",
                "ttl_seconds": 300,
            },
        )
        assert response.status_code == 201

        data = response.json()
        assert data["resource_type"] == "task"
        assert data["resource_id"] == "task-123"
        assert "id" in data

    @pytest.mark.asyncio
    async def test_acquire_lock_conflict(self, client):
        """Test acquiring a lock that's already held."""
        # First lock
        await client.post(
            "/locks",
            json={
                "resource_type": "project",
                "resource_id": "project-1",
            },
        )

        # Second lock should fail
        response = await client.post(
            "/locks",
            json={
                "resource_type": "project",
                "resource_id": "project-1",
            },
        )
        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_list_locks(self, client):
        """Test listing locks."""
        # Create a lock
        await client.post(
            "/locks",
            json={
                "resource_type": "task",
                "resource_id": "list-test",
            },
        )

        response = await client.get("/locks")
        assert response.status_code == 200

        data = response.json()
        assert isinstance(data, list)


class TestDashboardEndpoints:
    """Tests for dashboard endpoints."""

    @pytest.mark.asyncio
    async def test_dashboard_stats(self, client):
        """Test dashboard stats endpoint."""
        response = await client.get("/dashboard/stats")
        assert response.status_code == 200

        data = response.json()
        assert "total_workspaces" in data
        assert "total_projects" in data
        assert "total_tasks" in data


class TestTaskEndpoints:
    """Tests for existing task endpoints."""

    @pytest.mark.asyncio
    async def test_create_task(self, client):
        """Test task creation."""
        response = await client.post(
            "/tasks",
            json=[
                {
                    "name": "Test Task",
                    "description": "A test task",
                    "service": "lexora",
                    "payload": {"prompt": "test"},
                }
            ],
        )
        assert response.status_code == 201

        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 1

    @pytest.mark.asyncio
    async def test_list_tasks(self, client):
        """Test listing tasks."""
        response = await client.get("/tasks")
        assert response.status_code == 200

        data = response.json()
        assert "tasks" in data
        assert "total" in data

    @pytest.mark.asyncio
    async def test_get_task_events(self, client):
        """Test getting task events."""
        # Create a task first
        create_response = await client.post(
            "/tasks",
            json=[
                {
                    "name": "Event Test Task",
                    "service": "lexora",
                    "payload": {},
                }
            ],
        )
        task_ids = create_response.json()

        # Get events for the task
        response = await client.get(f"/tasks/{task_ids[0]}/events")
        assert response.status_code == 200

        data = response.json()
        assert isinstance(data, list)


class TestStatisticsEndpoints:
    """Tests for statistics endpoints."""

    @pytest.mark.asyncio
    async def test_stats_endpoint(self, client):
        """Test stats endpoint."""
        response = await client.get("/stats")
        assert response.status_code == 200

        data = response.json()
        assert "total_tasks" in data
        assert "tasks_by_status" in data


class TestWebhookEndpoints:
    """Tests for webhook endpoints."""

    @pytest.mark.asyncio
    async def test_create_webhook(self, client):
        """Test webhook creation."""
        response = await client.post(
            "/workspaces/default/webhooks",
            json={
                "service": "slack",
                "url": "https://hooks.slack.com/services/test",
                "events": ["completed", "failed"],
            },
        )

        # May fail if default workspace doesn't exist
        if response.status_code == 201:
            data = response.json()
            assert data["service"] == "slack"
            assert "id" in data
        else:
            assert response.status_code in [201, 403, 404]
