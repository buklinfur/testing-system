// Interactor protocol v1: interactor <input> [<answer>]
// stdin/stdout интерактора соединены со stdout/stdin решения участника.
// Exit codes: 0 = OK, 1 = WA, 2 = PE, 3 = FAIL. Сообщение пишем в stderr.
#include <fstream>
#include <iostream>
#include <string>
using namespace std;

static int wa(const string& msg) { cerr << msg << "\n"; return 1; }

int main(int argc, char** argv) {
    if (argc < 2) { cerr << "usage: interactor <input> [<answer>]\n"; return 3; }
    ifstream in(argv[1]);
    long long n, secret;
    if (!(in >> n >> secret)) { cerr << "bad input file\n"; return 3; }

    const int MAX_QUERIES = 32;
    cout << n << endl;  // endl сбрасывает буфер

    for (int q = 1; q <= MAX_QUERIES + 1; ++q) {
        long long x;
        if (!(cin >> x)) return wa("the solution stopped before guessing the number");
        if (q > MAX_QUERIES) return wa("too many queries (limit is 32)");
        if (x < 1 || x > n) return wa("query is out of range");
        if (x == secret) {
            cout << "EQUAL" << endl;
            cerr << "ok, queries: " << q << "\n";
            return 0;
        }
        cout << (secret < x ? "LESS" : "GREATER") << endl;
    }
    return wa("too many queries");
}
