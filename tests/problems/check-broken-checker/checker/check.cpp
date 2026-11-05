// checker input output answer: exit 0 = OK, 1 = WA
#include <fstream>
int main(int argc, char** argv) {
    std::ifstream out(argv[2]), ans(argv[3]);
    long long a, b
    out >> a; ans >> b;
    return a == b ? 0 : 1;
}
