#!/bin/bash

URL="https://gateway-voters.eci.gov.in/api/v1/printing-publish/get-publish-part-list"

curl -sS -i "$URL" \
  -H 'Accept: */*' \
  -H 'Origin: https://voters.eci.gov.in' \
  -H 'applicationname: VSP' \
  -H 'channelidobo: VSP' \
  -H 'content-type: application/json' \
  -H 'platform-type: ECIWEB' \
  --data-raw '{
    "encryptedPayload": "YOUR_ENCRYPTED_PAYLOAD",
    "encryptedKey": "YOUR_ENCRYPTED_KEY",
    "iv": "YOUR_IV"
  }'