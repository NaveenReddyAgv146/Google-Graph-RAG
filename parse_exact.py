import json

with open('/mnt/c/Users/Admin_Agivant/run_logs_exact.json', encoding='utf-8-sig') as f:
    data = json.load(f)

for entry in reversed(data):
    ts = entry.get('timestamp')
    log = entry.get('logName', '').split('/')[-1]
    txt = entry.get('textPayload')
    http = entry.get('httpRequest', {})
    req_size = http.get('requestSize')
    resp_size = http.get('responseSize')
    status = http.get('status')
    print(f"{ts} | {log} | status={status} | req_size={req_size} resp_size={resp_size} | {txt}")

