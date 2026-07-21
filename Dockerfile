# Apify's Python actor base image (Python 3.12).
FROM apify/actor-python:3.12

COPY requirements.txt ./
RUN echo "Python: $(python --version)" \
 && pip install --no-cache-dir -r requirements.txt \
 && echo "Installed:" && pip freeze

COPY . ./

# `python -m src` runs src/__main__.py
CMD ["python", "-m", "src"]
