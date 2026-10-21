// Checker protocol v1: check <input> <output> [<answer>]
// Exit codes: 0 = OK, 1 = WA, 2 = PE, 3 = FAIL (ошибка жюри). Сообщение пишем в stderr.
#include <fstream>
#include <iostream>
#include <string>
using namespace std;

int main(int argc, char** argv) {
    if (argc < 3) { cerr << "usage: check <input> <output> [<answer>]\n"; return 3; }
    ifstream in(argv[1]), out(argv[2]);
    if (!in) { cerr << "cannot open input file\n"; return 3; }
    long long n;
    if (!(in >> n)) { cerr << "bad input file\n"; return 3; }

    long long a, b;
    if (!(out >> a >> b)) { cerr << "expected two integers\n"; return 2; }
    string extra;
    if (out >> extra) { cerr << "extra data after the two numbers\n"; return 2; }

    const long long LIM = 2000000000LL;
    if (a < -LIM || a > LIM || b < -LIM || b > LIM) {
        cerr << "numbers must be in [-2e9, 2e9]\n"; return 1;
    }
    if (a + b != n) {
        cerr << "a + b = " << (a + b) << ", expected " << n << "\n"; return 1;
    }
    cerr << "ok\n";
    return 0;
}
