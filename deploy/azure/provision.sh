#!/usr/bin/env bash
# Azure resources for the TMP pipeline in resource group AI-CoE-rg (Simform-Dev): a Foundry (AIServices) resource with a
# project and model deployments. Tracing goes to Langfuse Cloud (LANGFUSE_* in ~/.config/tmp-pipeline/.env.azure), not Azure.
#
#   deploy/azure/provision.sh check     read-only: sign-in, resource group, what exists, the region's model catalogue
#   deploy/azure/provision.sh create    Foundry resource + project
#   deploy/azure/provision.sh deploy    model deployments listed in deploy/azure/models.tsv (skips existing ones)
#   deploy/azure/provision.sh search    Azure AI Search on the Free tier (50 MB, 3 indexes, no SLA; one per subscription)
#   deploy/azure/provision.sh cosmos    Cosmos DB free tier: chunks + vectors with hybrid search (the free search backend)
#   deploy/azure/provision.sh env       write ~/.config/tmp-pipeline/.env.azure (keys, endpoints, deployment map; keeps LANGFUSE_*) - mode 600
#
# Every step is idempotent: an existing resource is left as it is. Override names with the variables below.
# `az` on this machine runs in Docker (~/.local/bin/az) and only sees its own mounted folder, so nothing here passes
# files to az: bodies go inline and results come back on stdout.
set -euo pipefail

SUBSCRIPTION="${SUBSCRIPTION:-Simform-Dev}"
RG="${RG:-AI-CoE-rg}"
LOCATION="${LOCATION:-australiaeast}"
ACCOUNT="${ACCOUNT:-ai-coe-tmp-aue}"                 # Foundry (AIServices) resource; also its custom subdomain
PROJECT="${PROJECT:-tmp-pipeline}"
SEARCH="${SEARCH:-ai-coe-tmp-search}"               # AI Search service (Free tier); also its endpoint subdomain
SEARCH_SKU="${SEARCH_SKU:-free}"
COSMOS="${COSMOS:-ai-coe-tmp-cosmos}"               # Cosmos DB account (free tier); also its endpoint subdomain
COSMOS_DB="${COSMOS_DB:-tmp}"
COSMOS_CONTAINER="${COSMOS_CONTAINER:-chunks}"                    # basic (~US$97/month per unit) when the 50 MB Free limit is outgrown
HERE="$(cd "$(dirname "$0")" && pwd)"
MODELS="${MODELS:-$HERE/models.tsv}"
ENV_FILE="${ENV_FILE:-$HOME/.config/tmp-pipeline/.env.azure}"   # outside the repo: secrets never sit in the working tree
TAGS=(project=tmp-pipeline owner=apurv.panchal@simformsolutions.com)

say() { printf '\n== %s\n' "$*"; }
exists() { "$@" >/dev/null 2>&1; }

check() {
  say "account"
  az account set --subscription "$SUBSCRIPTION"
  az account show --query "{subscription:name, user:user.name}" -o table
  say "resource group $RG"
  az group show -n "$RG" --query "{name:name, location:location}" -o table
  say "what already exists in $RG"
  az resource list -g "$RG" --query "[].{name:name, type:type, location:location}" -o table
  say "providers"
  for p in Microsoft.CognitiveServices; do
    printf '%-32s %s\n' "$p" "$(az provider show -n "$p" --query registrationState -o tsv)"
  done
  say "model catalogue in $LOCATION (OpenAI chat and embedding models, GlobalStandard/Standard SKUs)"
  az cognitiveservices model list -l "$LOCATION" \
    --query "[?model.format=='OpenAI' && (starts_with(model.name,'gpt') || starts_with(model.name,'o') || contains(model.name,'embedding'))].{model:model.name, version:model.version, skus:join(',', model.skus[].name), retire:model.deprecation.inference}" \
    -o table | sort -u
}

create() {
  az account set --subscription "$SUBSCRIPTION"
  say "Foundry resource $ACCOUNT"
  if exists az cognitiveservices account show -n "$ACCOUNT" -g "$RG"; then
    echo "exists"
  else
    az cognitiveservices account create -n "$ACCOUNT" -g "$RG" -l "$LOCATION" --kind AIServices --sku S0 \
      --custom-domain "$ACCOUNT" --allow-project-management --assign-identity --tags "${TAGS[@]}" --yes -o none
  fi
  say "project $PROJECT"
  if exists az cognitiveservices account project show -n "$ACCOUNT" -g "$RG" --project-name "$PROJECT"; then
    echo "exists"
  else
    az cognitiveservices account project create -n "$ACCOUNT" -g "$RG" -l "$LOCATION" --project-name "$PROJECT" \
      --display-name "TMP pipeline" --description "TMP extraction, chunking and retrieval (Simform AI-COE)" -o none
  fi
}

deploy() {
  az account set --subscription "$SUBSCRIPTION"
  [ -f "$MODELS" ] || { echo "no $MODELS: run 'check' and fill it in" >&2; exit 2; }
  local failed=()
  # columns: deployment-name  model-name  model-version  sku  capacity(k TPM)   ('#' starts a comment)
  while read -r name model version sku capacity _plan; do
    [ -z "${name:-}" ] || [[ "$name" == \#* ]] && continue
    say "deployment $name -> $model $version ($sku, $capacity)"
    if exists az cognitiveservices account deployment show -n "$ACCOUNT" -g "$RG" --deployment-name "$name"; then
      echo "exists"
      continue
    fi
    az cognitiveservices account deployment create -n "$ACCOUNT" -g "$RG" --deployment-name "$name" \
      --model-name "$model" --model-version "$version" --model-format OpenAI --sku-name "$sku" \
      --sku-capacity "$capacity" -o none || failed+=("$name")    # quota errors name the limit; carry on
  done < "$MODELS"
  [ ${#failed[@]} -eq 0 ] || echo "FAILED: ${failed[*]}" >&2
  az cognitiveservices account deployment list -n "$ACCOUNT" -g "$RG" \
    --query "[].{deployment:name, model:properties.model.name, version:properties.model.version, sku:sku.name, capacity:sku.capacity, state:properties.provisioningState}" -o table
}

search() {
  az account set --subscription "$SUBSCRIPTION"
  say "AI Search $SEARCH ($SEARCH_SKU)"
  if exists az search service show -n "$SEARCH" -g "$RG"; then
    echo "exists"
  else
    az search service create -n "$SEARCH" -g "$RG" -l "$LOCATION" --sku "$SEARCH_SKU" --tags "${TAGS[@]}" -o none
  fi
  az search service show -n "$SEARCH" -g "$RG" --query "{name:name, sku:sku.name, status:status, location:location}" -o table
}

cosmos() {
  # Cosmos DB for NoSQL on the lifetime free tier (1000 RU/s, 25 GB; one per subscription): chunks with their vectors,
  # hybrid search (BM25 full text + DiskANN vectors, fused with RRF). One container with 1000 RU/s (the whole free
  # allowance; vector indexes are not allowed on shared-throughput databases), so it never bills. Add no other
  # throughput to this account. A taken free tier makes Azure refuse the account (nothing paid is created).
  az account set --subscription "$SUBSCRIPTION"
  say "Cosmos DB $COSMOS (free tier)"
  if exists az cosmosdb show -n "$COSMOS" -g "$RG"; then
    echo "exists"
  else
    az cosmosdb create -n "$COSMOS" -g "$RG" --enable-free-tier true --kind GlobalDocumentDB \
      --locations regionName="$LOCATION" failoverPriority=0 isZoneRedundant=False \
      --default-consistency-level Session --capabilities EnableNoSQLVectorSearch EnableNoSQLFullTextSearch \
      --tags "${TAGS[@]}" -o none
  fi
  say "database $COSMOS_DB (no shared throughput: vector indexes need a container's own)"
  if exists az cosmosdb sql database show -a "$COSMOS" -g "$RG" -n "$COSMOS_DB"; then
    echo "exists"
  else
    az cosmosdb sql database create -a "$COSMOS" -g "$RG" -n "$COSMOS_DB" -o none
  fi
  say "container $COSMOS_CONTAINER (1000 RU/s = the free allowance; partition /doc_id; vector + full-text indexes)"
  if exists az cosmosdb sql container show -a "$COSMOS" -g "$RG" -d "$COSMOS_DB" -n "$COSMOS_CONTAINER"; then
    echo "exists"
  else
    az cosmosdb sql container create -a "$COSMOS" -g "$RG" -d "$COSMOS_DB" -n "$COSMOS_CONTAINER" \
      --partition-key-path /doc_id --throughput 1000 \
      --vector-embeddings '{"vectorEmbeddings": [{"path": "/vector", "dataType": "float32", "dimensions": 3072, "distanceFunction": "cosine"}]}' \
      --full-text-policy '{"defaultLanguage": "en-US", "fullTextPaths": [{"path": "/text", "language": "en-US"}]}' \
      --idx '{"indexingMode": "consistent", "automatic": true, "includedPaths": [{"path": "/*"}], "excludedPaths": [{"path": "/vector/*"}, {"path": "/\"_etag\"/?"}], "vectorIndexes": [{"path": "/vector", "type": "diskANN"}], "fullTextIndexes": [{"path": "/text"}]}' \
      -o none
  fi
  az cosmosdb show -n "$COSMOS" -g "$RG" \
    --query "{name:name, freeTier:enableFreeTier, location:location, endpoint:documentEndpoint, capabilities:join(',', capabilities[].name)}" -o table
}

env_file() {
  az account set --subscription "$SUBSCRIPTION"
  local endpoint key map embed keep skey ckey
  endpoint="https://$ACCOUNT.openai.azure.com"
  key=$(az cognitiveservices account keys list -n "$ACCOUNT" -g "$RG" --query key1 -o tsv)
  # plan model name -> deployment: the 6th column of models.tsv says which plan name a deployment stands in for
  map=$(awk '!/^#/ && NF>=6 && $6!="-" {printf "%s%s=%s", (n++?",":""), $6, $1}' "$MODELS")
  embed=$(awk '!/^#/ && $2 ~ /embedding/ {print $1; exit}' "$MODELS")
  # tracing settings are yours (Langfuse keys): keep them across rewrites
  skey=$(az search admin-key show --service-name "$SEARCH" -g "$RG" --query primaryKey -o tsv 2>/dev/null || true)
  ckey=$(az cosmosdb keys list -n "$COSMOS" -g "$RG" --query primaryMasterKey -o tsv 2>/dev/null || true)
  keep=$( [ -f "$ENV_FILE" ] && grep -E '^(LANGFUSE_|TMP_TRACE_CONTENT)' "$ENV_FILE" || true)
  umask 077
  mkdir -p "$(dirname "$ENV_FILE")"
  cat > "$ENV_FILE" <<ENVEOF
# written by deploy/azure/provision.sh env - secrets, never commit (.gitignore has .env*)
# values are quoted
AZURE_OPENAI_ENDPOINT='$endpoint'
AZURE_OPENAI_KEY='$key'
AZURE_OPENAI_CHAT_DEPLOYMENTS='$map'
AZURE_OPENAI_EMBEDDING_DEPLOYMENT='$embed'
ENVEOF
  if [ -n "$ckey" ]; then
    printf "AZURE_COSMOS_ENDPOINT='https://%s.documents.azure.com:443/'\nAZURE_COSMOS_KEY='%s'\nAZURE_COSMOS_DATABASE='%s'\nAZURE_COSMOS_CONTAINER='%s'\n" \
      "$COSMOS" "$ckey" "$COSMOS_DB" "$COSMOS_CONTAINER" >> "$ENV_FILE"
  fi
  if [ -n "$skey" ]; then
    printf "AZURE_SEARCH_ENDPOINT='https://%s.search.windows.net'\nAZURE_SEARCH_KEY='%s'\n" "$SEARCH" "$skey" >> "$ENV_FILE"
  fi
  if [ -n "$keep" ]; then
    printf '%s\n' "$keep" >> "$ENV_FILE"
  else
    cat >> "$ENV_FILE" <<'ENVEOF'
# Langfuse Cloud tracing: keys from the Langfuse project settings; host by region
# (EU https://cloud.langfuse.com, US https://us.cloud.langfuse.com, JP https://jp.cloud.langfuse.com)
LANGFUSE_PUBLIC_KEY=''
LANGFUSE_SECRET_KEY=''
LANGFUSE_HOST='https://cloud.langfuse.com'
TMP_TRACE_CONTENT=1
ENVEOF
  fi
  echo "wrote $ENV_FILE (mode 600); load it with: set -a; . $ENV_FILE; set +a"
}

case "${1:-}" in
  check) check ;;
  create) create ;;
  deploy) deploy ;;
  search) search ;;
  cosmos) cosmos ;;
  env) env_file ;;
  *) sed -n '2,12p' "$0"; exit 2 ;;
esac
