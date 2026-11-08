#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

static int only_mapping(const char *path) {
    FILE *file = fopen(path, "r");
    unsigned long inside, outside, count;
    if (!file) return 0;
    int n = fscanf(file, "%lu %lu %lu", &inside, &outside, &count);
    int extra = 0;
    char c;
    while (fscanf(file, "%c", &c) == 1) if (c != ' ' && c != '\n' && c != '\t') extra = 1;
    fclose(file);
    return n == 3 && !extra && inside == 65534 && outside == 65534 && count == 1;
}
static int filtered(long number, long argument) {
    pid_t child = fork();
    if (child < 0) return 0;
    if (!child) { syscall(number, argument, 0, 0, 0, 0, 0); _exit(99); }
    int status;
    while (waitpid(child, &status, 0) < 0) if (errno != EINTR) return 0;
    return WIFSIGNALED(status) && WTERMSIG(status) == SIGSYS;
}

int main(int argc, char **argv) {
    static const char *names[] = {"net", "user", "pid", "mnt", "ipc", "uts", "cgroup"};
    if (argc != 8 || getuid() != 65534 || getgid() != 65534 || getgroups(0, NULL) != 0) return 1;
    if (prctl(PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0) != 1) return 2;
    for (int i = 0; i < 7; i++) {
        char path[64]; struct stat info;
        snprintf(path, sizeof(path), "/proc/self/ns/%s", names[i]);
        if (stat(path, &info) || info.st_ino == strtoul(argv[i+1], NULL, 10)) return 3;
    }
    if (!only_mapping("/proc/self/uid_map") || !only_mapping("/proc/self/gid_map")) return 4;
    for (int fd = 3; fd < 128; fd++) if (fcntl(fd, F_GETFD) != -1 || errno != EBADF) return 5;
    FILE *membership = fopen("/proc/self/cgroup", "r");
    char cgroup_line[128];
    if (!membership) return 16;
    int at_stage_root = fgets(cgroup_line, sizeof(cgroup_line), membership)
        && !strcmp(cgroup_line, "0::/\n") && fgetc(membership) == EOF;
    fclose(membership);
    if (!at_stage_root) return 16;
    FILE *file = fopen("/proc/self/status", "r");
    if (!file) return 6;
    char line[256]; int secure = 0, caps = 0;
    while (fgets(line, sizeof(line), file)) {
        unsigned long long value;
        if (sscanf(line, "CapEff: %llx", &value) == 1) { if (value) return 7; caps++; }
        if (sscanf(line, "CapPrm: %llx", &value) == 1) { if (value) return 7; caps++; }
        if (sscanf(line, "CapInh: %llx", &value) == 1) { if (value) return 7; caps++; }
        if (sscanf(line, "CapAmb: %llx", &value) == 1) { if (value) return 7; caps++; }
        if (sscanf(line, "CapBnd: %llx", &value) == 1) { if (value) return 7; caps++; }
        if (!strcmp(line, "Seccomp:\t2\n")) secure = 1;
    }
    fclose(file);
    if (!secure || caps != 5) return 8;
    struct statvfs filesystem;
    if (statvfs("/", &filesystem) || !(filesystem.f_flag & ST_RDONLY)) return 9;
    int fd = open("/rootfs-write-probe", O_WRONLY | O_CREAT, 0600);
    if (fd >= 0 || (errno != EROFS && errno != EACCES)) return 9;
    if (!access("/run/secrets", F_OK) || !access("/sys/fs/cgroup", F_OK)
        || !access("/run/checker-cgroup", F_OK)) return 10;
    file = fopen("/proc/net/dev", "r");
    if (!file) return 11;
    while (fgets(line, sizeof(line), file)) {
        char *colon = strchr(line, ':');
        if (colon) { *colon = 0; char *start = line; while (*start == ' ') start++; if (strcmp(start, "lo")) return 12; }
    }
    fclose(file);
    const char *environment[] = {"PATH=/usr/bin:/bin", "LANG=C", "LC_ALL=C", "HOME=/work", "TMPDIR=/work/tmp"};
    extern char **environ;
    int variables = 0;
    for (char **entry = environ; *entry; entry++) {
        int known = 0;
        for (int i = 0; i < 5; i++) if (!strcmp(*entry, environment[i])) known = 1;
        if (!known) return 13;
        variables++;
    }
    if (variables != 5) return 13;
    for (int n = 3; n < 128; n++) {
        char path[64];
        snprintf(path, sizeof(path), "/proc/1/fd/%d", n);
        if (open(path, O_RDONLY) >= 0) return 14;
    }
    /* Seccomp:2 alone could describe only the inherited OUTER filter.
     * These real forked probes must die under the installed INNER default-KILL policy. */
    if (!filtered(SYS_socket, AF_INET) || !filtered(SYS_unshare, CLONE_NEWUSER)
        || !filtered(SYS_ptrace, 0)) return 15;
    puts("CHECKER_JAIL_READY");
    fflush(stdout);
    return 0;
}
