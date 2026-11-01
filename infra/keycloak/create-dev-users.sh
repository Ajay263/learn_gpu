#!/usr/bin/env bash
# DEVELOPMENT ONLY: create test users in realm `newsintel` (idempotent). Never run against production.
#   analyst1 : analyst, collection-general
#   viewer2  : viewer,  collection-restricted
#   admin1   : admin,   collection-general + collection-restricted
set -euo pipefail
KC=/opt/keycloak/bin/kcadm.sh
$KC config credentials --server http://localhost:8080 --realm master --user "$KEYCLOAK_ADMIN_USER" --password "$KEYCLOAK_ADMIN_PASSWORD" >/dev/null

mkuser() { # username role groups...
  local u=$1 role=$2; shift 2
  if ! $KC get users -r newsintel -q "username=$u" -q exact=true --fields id | grep -q '"id"'; then
    $KC create users -r newsintel -s "username=$u" -s enabled=true -s "email=$u@example.invalid" \
      -s emailVerified=true -s "firstName=$u" -s lastName=dev >/dev/null
  fi
  $KC set-password -r newsintel --username "$u" --new-password "$DEV_USER_PASSWORD" >/dev/null
  $KC add-roles -r newsintel --uusername "$u" --rolename "$role" >/dev/null
  local uid; uid=$($KC get users -r newsintel -q "username=$u" -q exact=true --fields id --format csv --noquotes)
  for g in "$@"; do
    local gid; gid=$($KC get groups -r newsintel -q "search=$g" --fields id --format csv --noquotes | head -1)
    $KC update "users/$uid/groups/$gid" -r newsintel -s realm=newsintel -s "userId=$uid" -s "groupId=$gid" -n >/dev/null
  done
  echo "user $u: role=$role groups=$*"
}
mkuser analyst1 analyst collection-general
mkuser viewer2 viewer collection-restricted
mkuser admin1 admin collection-general collection-restricted
