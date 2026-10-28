"""Build the optional C++ text kernel; no production process is restarted."""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main():
    target = ROOT / 'src/warmagent/_native'
    target.mkdir(exist_ok=True)
    source = ROOT / 'native/text_kernels.cpp'
    if sys.platform == 'win32':
        command = [sys.executable, '-m', 'ziglang', 'c++', '-target', 'x86_64-windows-gnu',
                   '-O3', '-DNDEBUG', '-std=c++17', '-shared', '-fno-exceptions', '-fno-rtti',
                   '-nostdlib++',
                   str(source), '-o', str(target / 'text_kernels.dll')]
    else:
        command = ['c++', '-O3', '-DNDEBUG', '-std=c++17', '-shared', '-fPIC',
                   str(source), '-o', str(target / 'libtext_kernels.so')]
    with (target / 'build.log').open('w', encoding='utf-8') as log:
        subprocess.run(command, check=True, timeout=180, stdout=log, stderr=subprocess.STDOUT)
    print('C++ text kernel built in', target)


if __name__ == '__main__':
    main()
