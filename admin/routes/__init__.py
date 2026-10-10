from admin.auth import router as auth_router
from admin.routes.agent_config import router as agent_config_router
from admin.routes.billing import router as billing_router
from admin.routes.businesses import router as businesses_router
from admin.routes.conversations import router as conversations_router
from admin.routes.dashboard import router as dashboard_router
from admin.routes.entities import router as entities_router
from admin.routes.integrations import router as integrations_router
from admin.routes.knowledge import router as knowledge_router
from admin.routes.mcp_connections import router as mcp_connections_router
from admin.routes.routing import router as routing_router
from admin.routes.setup_links import router as setup_links_router
from admin.routes.simulator import router as simulator_router
from admin.routes.test_simulator import router as lab_simulator_router
from admin.routes.widget import router as widget_router
from admin.routes.workflows import router as workflows_router

ADMIN_ROUTERS = [
    auth_router,
    dashboard_router,
    businesses_router,
    integrations_router,
    mcp_connections_router,
    setup_links_router,
    knowledge_router,
    entities_router,
    workflows_router,
    conversations_router,
    agent_config_router,
    billing_router,
    routing_router,
    simulator_router,
    lab_simulator_router,
]
