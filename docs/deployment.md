# Deployment

The application ships as a single Docker image that runs the Streamlit frontend. The image is built and released to Azure by an Azure DevOps pipeline defined under `deploy/`.

## Container image

The `Dockerfile` is a two stage build. A `python:3.10-bookworm` builder installs the pinned dependencies from `requirements.txt`, and a slim `python:3.10-slim-bookworm` runtime carries only the installed packages and the application code. Because the internal `avb-llm` and `avb-search` packages come from the a.s.r. Azure Artifacts feeds, the build must run inside the a.s.r. network with `PIP_INDEX_URL` and `PIP_EXTRA_INDEX_URL` set to those feeds.

The image accepts build arguments for `ACCOUNT_NAME`, `DEFAULT_IDENTITY_CLIENT_ID`, `AZURE_ML_SUBSCRIPTION_ID`, `APP_PORT`, and `LOG_LEVEL`, exposes ports `443` and `8501`, and starts the app through its launcher:

```dockerfile
ENTRYPOINT ["sh", "-c", "python ./equities_frontend/main.py --port ${APP_PORT}"]
```

To build and run the image locally against the internal feed:

```bash
docker build \
  --build-arg APP_PORT=8501 \
  --build-arg ACCOUNT_NAME=weavbaostoredls01 \
  -t equities_frontend .

docker run --rm -p 8501:8501 --env-file .env equities_frontend
```

## Azure DevOps pipeline

`deploy/azure_pipeline.yaml` builds the image, pushes it to the Azure Container Registry, and deploys it to an Azure Web App. It triggers on the `production` branch and consumes shared build templates from the `Vermogensbeheer.Innersource/templates_pipelines@production` repository, running on the `asr-shared-p-linux` pool. The image name is `equities_frontend`, set in `deploy/pipeline_variables.yaml`.

The branch determines the deploy target, which in turn selects the resource group and web app name.

| Deploy target | Resource group | Web app name |
|---------------|----------------|--------------|
| `p` (production) | `we-avba-p-wapp-rg` | `weavbapwappaandelenaidashboard01` |
| `a` (acceptance) | `we-avba-a-wapp-rg` | `weavbaawappaandelenaidashboard01` |
| `o` (development) | `we-avba-o-wapp-rg` | `weavbaowappaandelenaidashboard01` |

The build and deploy work is delegated to the shared `stages/BuildAndDeployToAcr.yaml@templates` stage, which is given the resource group, the web app name, the image name, and the `Dockerfile` path. Aligning the deploy target with the environment blocks in `app_config.yaml` keeps the running container pointed at the correct Data Lake and search resources for its stage.
