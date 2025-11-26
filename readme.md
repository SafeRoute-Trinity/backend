## Create a virtual environment for Python3
```python3 -m venv venv```

```source venv/bin/activate```

## Install necessary packages

```pip install -r requirements.txt```

## Turn on the development hooks

```pre-commit install```

## Run the backend

```uvicorn main:app --reload```