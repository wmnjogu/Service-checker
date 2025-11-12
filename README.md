# Service Monitoring API

## Overview
This project is a Flask-based API for monitoring the availability of various services. It periodically checks the status of configured services and sends alerts to a Lark group if any service goes offline or comes back online.

### Features
- Monitor multiple services via HTTP requests.
- Send alerts to a Lark group using a webhook.
- Start and stop monitoring via API endpoints.
- Update the list of monitored services dynamically.
- Log service status and API actions.

### Technologies Used
Python (Flask, Requests, Logging, Threading)
Lark Webhook for notifications
RESTful API
python-dotenv

# Installation
### Prerequisites
Ensure you have Python installed on your system. Then, install the required dependencies:
```
pip install flask requests
```
Running the Application
```
python app.py 
```
The server will start on http://0.0.0.0:5001.

# API Endpoints
## Start Monitoring
Endpoint:
```
POST /start-monitoring
```
Response:
```
{"message": "Monitoring started."}
```

## Stop Monitoring
Endpoint:
```
POST /stop-monitoring
```
Response:
```
{"message": "Monitoring stopped."}
```
## Check Monitoring Status
Endpoint:
```
GET /status
```
Response:
```
{
  "monitoring_running": true,
  "services": {
    "InfluxDB GPON": {"url": "http://105.29.165.232:24004/ping", "alert_sent": false}
  }
}
```
# Update Monitored Services
Endpoint:
```
POST /update-services
```
Request Body:
```
{
  "services": {
    "New Service": "http://example.com/ping"
  }
}
```
Response:
```
{"message": "Services updated."}
```
## Logging
All service status checks and API interactions are logged in service_monitor.log.

## Notes
- The application runs a background thread for continuous monitoring.
- Alerts are sent only when a service goes down or comes back up.