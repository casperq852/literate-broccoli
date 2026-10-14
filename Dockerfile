FROM python:3.10-bookworm as builder

ENV PYTHONUNBUFFERED 1

WORKDIR /app

COPY requirements.txt ./requirements.txt

ARG PIP_INDEX_URL
ENV PIP_INDEX_URL=$PIP_INDEX_URL
ARG PIP_EXTRA_INDEX_URL
ENV PIP_EXTRA_INDEX_URL=$PIP_EXTRA_INDEX_URL

RUN pip install --index-url ${PIP_INDEX_URL} \
                --extra-index-url ${PIP_EXTRA_INDEX_URL} \
                --no-cache-dir \
                --no-deps \
                --upgrade -r requirements.txt

FROM python:3.10-slim-bookworm as runtime

WORKDIR /app

ARG ACCOUNT_NAME
ENV ACCOUNT_NAME=$ACCOUNT_NAME
ARG DEFAULT_IDENTITY_CLIENT_ID
ENV DEFAULT_IDENTITY_CLIENT_ID=$DEFAULT_IDENTITY_CLIENT_ID

# Restrict DefaultAzureCredential (avb_core's auth fallback) to the "prod" set
# (EnvironmentCredential, WorkloadIdentityCredential, ManagedIdentityCredential).
# Without this it also probes AzureCliCredential / AzurePowerShellCredential /
# AzureDeveloperCliCredential first on every credential resolution -- none of
# which can ever succeed in this headless container, but each one still burns
# real time timing out before falling through to the one that works. That cost
# is paid repeatedly (e.g. on every chat-session autosave), not just once.
ENV AZURE_TOKEN_CREDENTIALS=prod

ARG AZURE_ML_SUBSCRIPTION_ID
ENV AZURE_ML_SUBSCRIPTION_ID=$amlSubscriptionID
ARG APP_PORT
ENV APP_PORT=$APP_PORT
ARG LOG_LEVEL
ENV LOG_LEVEL=$LOG_LEVEL

COPY --from=builder /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY equities_frontend/ ./equities_frontend/

ENV PYTHONPATH=/app

# Matplotlib builds its font cache on first import if it doesn't find one --
# often 10s+ on a constrained host, and it would otherwise happen at every
# container start/restart since nothing else warms it. Baking it into this
# image layer at build time means it's already there for every container run.
ENV MPLCONFIGDIR=/app/.cache/matplotlib
RUN python -c "import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot"

EXPOSE 443 8501

ENTRYPOINT ["sh", "-c", "python ./equities_frontend/main.py --port ${APP_PORT}"]
