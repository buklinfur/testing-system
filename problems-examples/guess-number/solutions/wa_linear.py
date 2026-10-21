n = int(input())
for q in range(1, n + 1):
    print(q, flush=True)
    if input().strip() == "EQUAL":
        break
