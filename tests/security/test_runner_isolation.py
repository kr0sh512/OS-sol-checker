"""Opt-in: all C compilation/execution happens ONLY in the explicitly configured runner.

Use a dedicated empty runner on the provisioned rootful reference host. Ordinary unit
runs skip this suite; a skipped suite is not evidence that sandbox security passed.
"""
import os
from uuid import uuid4

import pytest

from os_sol_checker.adapters.runner.client import RunnerClient
from os_sol_checker.config import Settings

pytestmark = pytest.mark.skipif(os.environ.get("RUNNER_SECURITY_OPT_IN") != "1",
                                reason="requires explicitly provisioned real nsjail runner, never native host C")


async def check(source, stdin=b""):
    settings = Settings(runner_url=os.environ["RUNNER_SECURITY_URL"],
                        runner_token=os.environ["RUNNER_SECURITY_TOKEN"])
    client = RunnerClient(settings)
    handle = None
    try:
        policy = await client.policy()
        assert not await client.executions(), "Isolation tests require a dedicated empty runner"
        handle = await client.open(uuid4(), policy, min(30, policy.limits.submission_deadline_seconds))
        compiled = await client.compile(handle, source)
        if compiled.status != "completed":
            return policy, compiled
        return policy, await client.run(handle, compiled.artifact_handle, stdin)
    finally:
        if handle is not None:
            await client.destroy(handle)
        await client.close()


async def test_runner_real_binary_stdio_fork_wait_pipe_and_threads():
    source = br'''
#include <pthread.h>
#include <sys/wait.h>
#include <unistd.h>
static void *worker(void *p) { return p; }
int main(void) {
    pthread_t t; if(pthread_create(&t,0,worker,0) || pthread_join(t,0)) return 1;
    int f[2]; if(pipe(f)) return 2;
    int pid=fork(); if(pid<0) return 3;
    if(!pid) { close(f[0]); char b[1024]; int n;
        while((n=read(0,b,sizeof b))>0) if(write(f[1],b,n)!=n) _exit(4);
        close(f[1]); _exit(0); }
    close(f[1]); char b[1024]; int n;
    while((n=read(f[0],b,sizeof b))>0) if(write(1,b,n)!=n) return 5;
    int s; waitpid(pid,&s,0); return !WIFEXITED(s)||WEXITSTATUS(s);
}
'''
    raw = b"\x00\xff\x80\r\n " * 100
    _, outcome = await check(source, raw)
    assert outcome.status == "completed", outcome
    assert outcome.stdout == raw


async def test_runner_real_cannot_read_control_secrets_or_inherit_extra_fds():
    source = br'''
#include <stdlib.h>
#include <fcntl.h>
#include <unistd.h>
#include <errno.h>
#include <stdio.h>
int main(void) {
    if(getuid()!=65534 || getgid()!=65534 || getgroups(0,0)!=0) return 1;
    if(getenv("RUNNER_TOKEN") || getenv("TRANSIENT_DATABASE_URL")) return 2;
    if(open("/run/secrets/runner_token",O_RDONLY)>=0 || open("/opt/checker/runner-manifest.json",O_RDONLY)>=0) return 3;
    if(open("/run/checker-cgroup/cgroup.procs",O_RDONLY)>=0) return 4;
    for(int fd=3;fd<128;fd++) if(fcntl(fd,F_GETFD)!=-1 || errno!=EBADF) return 5;
    for(int fd=3;fd<128;fd++){char p[64];snprintf(p,sizeof p,"/proc/1/fd/%d",fd);if(open(p,O_WRONLY)>=0)return 6;}
    return 0;
}
'''
    _, outcome = await check(source)
    assert outcome.status == "completed", outcome


async def test_runner_real_network_syscall_is_not_authorized():
    source = b"#include <sys/socket.h>\nint main(void){ return socket(AF_INET,SOCK_STREAM,0)>=0 ? 0 : 1; }"
    _, outcome = await check(source)
    assert outcome.status == "runtime_error"
    assert outcome.signal == 31  # SIGSYS from the inner default-KILL_PROCESS policy.


async def test_runner_real_streaming_output_limit_is_combined_and_bounded():
    source = br'''
#include <unistd.h>
int main(void){char b[4096]={0};for(;;){write(1,b,sizeof b);write(2,b,sizeof b);}}
'''
    policy, outcome = await check(source)
    assert outcome.status == "output_limit"
    assert outcome.resource_reason == "output"
    assert len(outcome.stdout) + len(outcome.stderr) <= policy.limits.output_bytes


async def test_runner_real_watchdog_stops_infinite_loop_and_descendants():
    source = br'''
#include <unistd.h>
int main(void){if(!fork()){setsid();for(;;)pause();}for(;;)pause();}
'''
    policy, outcome = await check(source)
    assert outcome.status == "run_timeout"
    assert outcome.wall_seconds < policy.limits.run_timeout_seconds + 8
    # check()'s finally demands acknowledged DELETE after cgroup.kill/cleanup, not merely a disconnected socket.


async def test_runner_real_cgroup_memory_is_not_guessed_from_exit_code():
    source = br'''
#include <stdlib.h>
int main(void){for(;;){volatile char *p=malloc(1048576);if(!p) return 1;for(int j=0;j<1048576;j+=4096)p[j]=1;}}
'''
    _, outcome = await check(source)
    assert outcome.status == "memory_limit"
    assert outcome.resource_reason == "memory"


async def test_runner_real_pid_limit_allows_handled_eagain():
    source = br'''
#include <sys/wait.h>
#include <signal.h>
#include <stdio.h>
#include <unistd.h>
int main(void){int ids[512],n=0;while(n<512){int p=fork();if(p<0)break;if(!p){for(;;)pause();}ids[n++]=p;}
for(int i=0;i<n;i++)kill(ids[i],SIGKILL);for(int i=0;i<n;i++)waitpid(ids[i],0,0);printf("%d\n",n);return 0;}
'''
    policy, outcome = await check(source)
    assert outcome.status == "completed", outcome
    assert 0 < int(outcome.stdout) < policy.limits.pids


async def test_runner_real_artifact_limit_is_not_an_unbounded_binary_channel():
    source = br'''__asm__(".section .rodata\n.space 1100000\n.text"); int main(void){return 0;}'''
    _, outcome = await check(source)
    assert outcome.status == "compile_error"
    assert outcome.artifact_handle is None
