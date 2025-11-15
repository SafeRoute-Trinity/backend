## Create a virtual environment for Python
```python3 -m venv venv```

```source venv/bin/activate```

## Install necessary packages

```pip install -r requirements_dev.txt```

## Turn on the development hooks

```pre-commit install --hook-type pre-commit```
```pre-commit install --hook-type pre-push```

## Run the backend

```uvicorn main:app --reload```

## Unit Test

```pytest```