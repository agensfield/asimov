#import <Foundation/Foundation.h>
#include <unistd.h>
#include <stdio.h>
#include <string.h>

#ifndef ASIMOV_EXECUTABLE
#define ASIMOV_EXECUTABLE "/opt/homebrew/opt/asimov/bin/asimov"
#endif
#ifndef ASIMOV_NOTIFICATION_EXECUTABLE
#define ASIMOV_NOTIFICATION_EXECUTABLE "/usr/bin/osascript"
#endif

// Remain the native responsible process while the existing reconciler runs.
// Never forward arbitrary arguments or provide a general-purpose FDA shell.
int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc == 2 && strcmp(argv[1], "--version") == 0) {
            puts("Asimov background launcher 1");
            return 0;
        }
        BOOL check = argc == 2 && strcmp(argv[1], "--check-identity") == 0;
        if (argc != 1 && !check) {
            fputs("Usage: asimov-background [--check-identity|--version]\n", stderr);
            return 64;
        }
        if (geteuid() == 0) {
            fputs("Asimov Background must run as the normal user.\n", stderr);
            return 77;
        }
        NSString *program = [NSBundle mainBundle].infoDictionary[@"AsimovExecutable"];
        if (program == nil) program = @ASIMOV_EXECUTABLE;
        if (![program isAbsolutePath]) {
            fputs("Asimov executable must be an absolute path.\n", stderr);
            return 78;
        }
        NSTask *task = [[NSTask alloc] init];
        task.executableURL = [NSURL fileURLWithPath:check ? @"/usr/bin/sudo" : program];
        // Exercise the same restricted helper/attribution chain as real updates.
        // The fixed preflight opens a protected file read-only and reads no data.
        task.arguments = check ? @[@"-n", @"/usr/bin/python3", @"-I", @"/Library/PrivilegedHelperTools/agensfield.asimov.path-exclusions.py"]
                               : @[@"reconcile", @"--json"];
        NSPipe *input = check ? [NSPipe pipe] : nil;
        if (check) task.standardInput = input;
        NSError *error = nil;
        if (![task launchAndReturnError:&error]) {
            fprintf(stderr, "Asimov Background: %s\n", error.localizedDescription.UTF8String);
            return 71;
        }
        if (check) {
            [input.fileHandleForWriting writeData:[@"{\"check_identity\":true}" dataUsingEncoding:NSUTF8StringEncoding]];
            [input.fileHandleForWriting closeFile];
        }
        [task waitUntilExit];
        int status = task.terminationReason == NSTaskTerminationReasonExit
                   ? task.terminationStatus : 128 + task.terminationStatus;
        if (status != 0 && !check) {
            fputs("Asimov reconciliation failed; inspect ~/.local/state/asimov/last-failure.json\n", stderr);
            NSTask *notification = [[NSTask alloc] init];
            notification.executableURL = [NSURL fileURLWithPath:@ASIMOV_NOTIFICATION_EXECUTABLE];
            notification.arguments = @[@"-e", @"display notification \"Backup exclusions need attention. Check ~/.local/state/asimov/last-failure.json and the Asimov service logs.\" with title \"Asimov reconciliation failed\""];
            if ([notification launchAndReturnError:NULL]) [notification waitUntilExit];
        }
        return status;
    }
}
