# Third-party notices

MendPilot builds on [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent), pinned to `mini-swe-agent==2.4.6`. Its DefaultAgent loop, model parsing/history and Docker execution are used through the installed package. MendPilot adds repair tools, context budgets, finalization, user interfaces and its own evaluation entry point. The mini baseline implementation is not vendored or executed by this project.

mini-swe-agent — MIT, Copyright (c) 2025 Kilian A. Lieret and Carlos E. Jimenez.

The tool workflow was informed by [SWE-agent](https://github.com/SWE-agent/SWE-agent). SWE-agent is not a runtime dependency and its source is not bundled.
SWE-agent — MIT, Copyright (c) 2024 John Yang, Carlos E. Jimenez, Alexander Wettig, Shunyu Yao, Karthik Narasimhan, Ofir Press.

[SWE-bench](https://github.com/SWE-bench/SWE-bench) is an optional evaluation dependency. Task repositories, datasets, container images, and model weights retain their respective licenses; they are not redistributed here. Model names in historical results are service identifiers and do not imply endorsement or independently verified weight versions.

The MIT permission text applicable to the above MIT projects is reproduced below. Each upstream distribution retains its own copyright and license files.

```text
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
