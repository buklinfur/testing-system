n = int(input())
lo, hi = 1, n
while True:
    mid = (lo + hi) // 2
    print(mid, flush=True)
    r = input().strip()
    if r == "EQUAL":
        break
    if r == "LESS":
        hi = mid - 1
    else:
        lo = mid + 1
