#include <cstdio>
int main(int argc, char** argv) {
    FILE* in = fopen(argv[1], "r");
    int x; fscanf(in, "%d", &x)
    printf("%d\n", x); fflush(stdout);
    int y; return (scanf("%d", &y) == 1 && y == x) ? 0 : 1;
}
