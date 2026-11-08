#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

/* The only process retaining the status pipe. Student code cannot forge this channel:
 * its copy is closed before exec, ptrace/process_vm/pidfd_getfd are filtered, and the
 * monitor is non-dumpable (including /proc/1/fd ptrace access checks). As PID1 it also
 * reaps descendants and is not a killable same-UID status oracle for the program. */
int main(int argc, char **argv) {
    if (argc < 5 || strcmp(argv[1], "--status-fd") || strcmp(argv[3], "--")) return 125;
    int status_fd = atoi(argv[2]);
    if (status_fd < 3 || prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)) return 125;
    int errors[2];
    if (pipe2(errors, O_CLOEXEC)) return 125;
    pid_t child = fork();
    if (child < 0) { dprintf(status_fd, "ERROR fork\n"); return 125; }
    if (!child) {
        close(status_fd);
        close(errors[0]);
        execv(argv[4], &argv[4]);
        int failure = errno;
        (void)!write(errors[1], &failure, sizeof(failure));
        _exit(127);
    }
    close(errors[1]);
    int failure = 0;
    ssize_t got;
    do { got = read(errors[0], &failure, sizeof(failure)); } while (got < 0 && errno == EINTR);
    close(errors[0]);
    if (got != 0) { dprintf(status_fd, "EXEC_ERROR %d\n", failure); return 125; }
    if (dprintf(status_fd, "START\n") < 0) return 125;
    int status;
    pid_t waited;
    do { waited = waitpid(child, &status, 0); } while (waited < 0 && errno == EINTR);
    if (waited != child) return 125;
    int code = WIFEXITED(status) ? WEXITSTATUS(status) : -1;
    int signum = WIFSIGNALED(status) ? WTERMSIG(status) : 0;
    if (dprintf(status_fd, "EXIT %d %d\n", code, signum) < 0) return 125;
    close(status_fd);
    /* Exiting PID1 destroys and reaps the remaining processes of this PID namespace.
     * The outer controller additionally kills/confirms the whole stage cgroup. */
    return 0;
}
