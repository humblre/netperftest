# netperftest

A tiny script that measures latency and upload/download speeds for a specified destination.
No parallel mechanisms are used to ensure that a single thread has exclusive use of the bandwidth.

You can also use it as a CLI like the following example
</br>
`python3 -m netperftest --type latency --host naver.com --port 443 --runs 10`,
</br>
which prints results being of the form
</br>
`{Ave Elapsed Seconds} {Standard Deviation}`.
</br>

For more details, use the option --help.
