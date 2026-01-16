/**
 * Magickit Dashboard JavaScript
 * Handles WebSocket connections, modals, and UI interactions
 */

// Global WebSocket connection
let ws = null;
let wsReconnectAttempts = 0;
const MAX_RECONNECT_ATTEMPTS = 5;
const RECONNECT_DELAY = 3000;

/**
 * Show a modal by ID
 */
function showModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
        modal.classList.remove('hidden');
    }
}

/**
 * Hide a modal by ID
 */
function hideModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
        modal.classList.add('hidden');
    }
}

/**
 * Close modal when clicking outside content
 */
document.addEventListener('click', function(e) {
    if (e.target.classList.contains('modal')) {
        e.target.classList.add('hidden');
    }
});

/**
 * Close modal on Escape key
 */
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        document.querySelectorAll('.modal:not(.hidden)').forEach(function(modal) {
            modal.classList.add('hidden');
        });
    }
});

/**
 * Connect to WebSocket for real-time updates
 */
function connectWebSocket() {
    // Get WebSocket URL from current location
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/projects/default`;

    console.log('Connecting to WebSocket:', wsUrl);

    ws = new WebSocket(wsUrl);

    ws.onopen = function() {
        console.log('WebSocket connected');
        wsReconnectAttempts = 0;
    };

    ws.onmessage = function(event) {
        const data = JSON.parse(event.data);
        handleWebSocketMessage(data);
    };

    ws.onclose = function() {
        console.log('WebSocket disconnected');
        attemptReconnect();
    };

    ws.onerror = function(error) {
        console.error('WebSocket error:', error);
    };
}

/**
 * Connect to WebSocket for a specific project
 */
function connectProjectWebSocket(projectId) {
    if (ws) {
        ws.close();
    }

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/projects/${projectId}`;

    console.log('Connecting to project WebSocket:', wsUrl);

    ws = new WebSocket(wsUrl);

    ws.onopen = function() {
        console.log('Project WebSocket connected:', projectId);
        wsReconnectAttempts = 0;
    };

    ws.onmessage = function(event) {
        const data = JSON.parse(event.data);
        handleWebSocketMessage(data);
    };

    ws.onclose = function() {
        console.log('Project WebSocket disconnected');
        // Don't auto-reconnect for project-specific connections
    };

    ws.onerror = function(error) {
        console.error('Project WebSocket error:', error);
    };
}

/**
 * Attempt to reconnect WebSocket
 */
function attemptReconnect() {
    if (wsReconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
        console.log('Max reconnect attempts reached');
        return;
    }

    wsReconnectAttempts++;
    console.log(`Attempting to reconnect (${wsReconnectAttempts}/${MAX_RECONNECT_ATTEMPTS})...`);

    setTimeout(function() {
        connectWebSocket();
    }, RECONNECT_DELAY);
}

/**
 * Handle incoming WebSocket messages
 */
function handleWebSocketMessage(data) {
    console.log('WebSocket message:', data);

    switch (data.type) {
        case 'connected':
            showNotification('Connected to real-time updates', 'success');
            break;

        case 'task_event':
            handleTaskEvent(data);
            break;

        case 'pong':
            // Heartbeat response
            break;

        default:
            console.log('Unknown message type:', data.type);
    }
}

/**
 * Handle task event from WebSocket
 */
function handleTaskEvent(data) {
    const eventType = data.event_type;
    const taskId = data.task_id;

    // Show notification
    let message = '';
    let type = 'info';

    switch (eventType) {
        case 'created':
            message = `Task created: ${taskId.substring(0, 8)}...`;
            type = 'info';
            break;
        case 'started':
            message = `Task started: ${taskId.substring(0, 8)}...`;
            type = 'info';
            break;
        case 'completed':
            message = `Task completed: ${taskId.substring(0, 8)}...`;
            type = 'success';
            break;
        case 'failed':
            message = `Task failed: ${taskId.substring(0, 8)}...`;
            type = 'error';
            break;
        case 'cancelled':
            message = `Task cancelled: ${taskId.substring(0, 8)}...`;
            type = 'warning';
            break;
    }

    if (message) {
        showNotification(message, type);
    }

    // Trigger HTMX refresh of task list
    const tasksList = document.getElementById('tasks-list');
    if (tasksList) {
        htmx.trigger(tasksList, 'htmx:load');
    }

    // Refresh events list
    const eventsList = document.getElementById('events-list');
    if (eventsList) {
        htmx.trigger(eventsList, 'htmx:load');
    }
}

/**
 * Show a notification toast
 */
function showNotification(message, type) {
    // Create notification element
    const notification = document.createElement('div');
    notification.className = `notification notification-${type}`;
    notification.textContent = message;

    // Style the notification
    notification.style.cssText = `
        position: fixed;
        bottom: 20px;
        right: 20px;
        padding: 12px 20px;
        border-radius: 8px;
        color: white;
        font-size: 14px;
        z-index: 9999;
        animation: slideIn 0.3s ease;
    `;

    // Set background color based on type
    const colors = {
        success: '#10B981',
        error: '#EF4444',
        warning: '#F59E0B',
        info: '#3B82F6'
    };
    notification.style.backgroundColor = colors[type] || colors.info;

    // Add to document
    document.body.appendChild(notification);

    // Remove after 3 seconds
    setTimeout(function() {
        notification.style.animation = 'slideOut 0.3s ease';
        setTimeout(function() {
            notification.remove();
        }, 300);
    }, 3000);
}

// Add CSS animation
const style = document.createElement('style');
style.textContent = `
    @keyframes slideIn {
        from {
            transform: translateX(100%);
            opacity: 0;
        }
        to {
            transform: translateX(0);
            opacity: 1;
        }
    }
    @keyframes slideOut {
        from {
            transform: translateX(0);
            opacity: 1;
        }
        to {
            transform: translateX(100%);
            opacity: 0;
        }
    }
`;
document.head.appendChild(style);

/**
 * Format date for display
 */
function formatDate(dateString) {
    const date = new Date(dateString);
    const now = new Date();
    const diff = now - date;

    // Less than a minute
    if (diff < 60000) {
        return 'Just now';
    }

    // Less than an hour
    if (diff < 3600000) {
        const minutes = Math.floor(diff / 60000);
        return `${minutes}m ago`;
    }

    // Less than a day
    if (diff < 86400000) {
        const hours = Math.floor(diff / 3600000);
        return `${hours}h ago`;
    }

    // Default format
    return date.toLocaleDateString() + ' ' + date.toLocaleTimeString();
}

/**
 * Heartbeat to keep WebSocket alive
 */
setInterval(function() {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'ping' }));
    }
}, 30000);

/**
 * Initialize on page load
 */
document.addEventListener('DOMContentLoaded', function() {
    console.log('Magickit Dashboard initialized');

    // Connect WebSocket if on dashboard
    if (window.location.pathname.startsWith('/dashboard')) {
        // Delay connection slightly to allow page to render
        setTimeout(connectWebSocket, 500);
    }
});
