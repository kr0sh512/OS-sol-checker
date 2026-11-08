/* Trusted build-time check: compile every policy with the exact pinned kafel. */
#include <kafel.h>
#include <linux/filter.h>
#include <stdio.h>
#include <stdlib.h>

int main(int argc, char **argv) {
    if (argc < 2) return 2;
    for (int i = 1; i < argc; ++i) {
        FILE *file = fopen(argv[i], "r");
        if (!file) { perror(argv[i]); return 1; }
        kafel_ctxt_t context = kafel_ctxt_create();
        if (!context) { fclose(file); return 1; }
        struct sock_fprog program = {0};
        kafel_set_input_file(context, file);
        kafel_set_target_archs(context, KAFEL_TARGET_ARCH_X86_64);
        int result = kafel_compile(context, &program);
        if (result) fprintf(stderr, "%s: %s\n", argv[i], kafel_error_msg(context));
        free(program.filter);
        kafel_ctxt_destroy(&context);
        fclose(file);
        if (result) return 1;
    }
    return 0;
}
