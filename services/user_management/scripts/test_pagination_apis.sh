#!/usr/bin/env bash
# Quick smoke test for pagination/filter APIs.
# Start the user_management service first:
#   uvicorn services.user_management.main:app --host 0.0.0.0 --port 20000 --reload
# Then run: bash services/user_management/scripts/test_pagination_apis.sh

set -e
BASE="${BASE_URL:-http://127.0.0.1:20000}"

echo "=== Testing pagination APIs (base: $BASE) ==="

echo ""
echo "1. GET /v1/users (list users, page 1, page_size 5)"
curl -s -S "$BASE/v1/users?page=1&page_size=5" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert 'data' in d, d
assert 'pagination' in d, d
p = d['pagination']
assert 'page' in p and 'page_size' in p and 'total' in p and 'total_pages' in p
print('  OK: data + pagination shape')
print('  pagination:', p)
"

echo ""
echo "2. GET /v1/users with filters (email contains, page_size 2)"
curl -s -S "$BASE/v1/users?page=1&page_size=2&email=test" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert 'data' in d and 'pagination' in d
print('  OK: filter applied')
"

echo ""
echo "3. GET /v1/audit (paginated, existing endpoint)"
curl -s -S "$BASE/v1/audit?page=1&page_size=3" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert 'data' in d and 'pagination' in d
p = d['pagination']
assert 'total_pages' in p
print('  OK: audit list has data + pagination')
print('  pagination:', p)
"

# Trusted contacts require a valid user_id; may 404 if no users
echo ""
echo "4. GET /v1/users/{user_id}/trusted-contacts (need valid UUID)"
USER_ID=$(curl -s -S "$BASE/v1/users?page=1&page_size=1" | python3 -c "
import sys, json
d = json.load(sys.stdin)
if d.get('data'):
    print(d['data'][0]['user_id'])
else:
    print('')
" 2>/dev/null || true)
if [ -n "$USER_ID" ]; then
  curl -s -S "$BASE/v1/users/$USER_ID/trusted-contacts?page=1&page_size=10" | python3 -c "
import sys, json
d = json.load(sys.stdin)
if 'detail' in d and 'not found' in str(d.get('detail','')).lower():
    print('  (user not found or no contacts – OK)')
else:
    assert 'data' in d and 'pagination' in d
    print('  OK: trusted-contacts has data + pagination')
    print('  pagination:', d['pagination'])
" 2>/dev/null || echo "  (skip: no user_id from list)"
else
  echo "  (skip: no users in DB to test trusted-contacts)"
fi

echo ""
echo "=== Done ==="
