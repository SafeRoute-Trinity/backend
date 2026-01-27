## Create a virtual environment for Python3
```python3 -m venv venv```

```source venv/bin/activate```

## Install necessary packages

```pip install -r requirements.txt```

## Turn on the development hooks

```pre-commit install```

## Run the backend

### Start all services
```bash
python main.py
```

This will start all microservices:
- User Management (port 20000)
- Notification (port 20001)
- Routing Service (port 20002)
- Safety Scoring (port 20003)
- Feedback (port 20004)
- Data Cleaner (port 20005)
- SOS (port 20006)

### Start a single service
```bash
python main.py --service feedback
```

### List available services
```bash
python main.py --list
```

### Individual service commands
You can also run services individually:
```bash
# Feedback service
uvicorn services.feedback.main:app --host 0.0.0.0 --port 20004 --reload

# User Management service
uvicorn services.user_management.main:app --host 0.0.0.0 --port 20000 --reload

# ... and so on for other services
```

### Service Discovery
The service discovery endpoint is available at:
```bash
uvicorn docs.main:app --host 0.0.0.0 --port 8080 --reload
```
Then visit: http://127.0.0.1:8080/