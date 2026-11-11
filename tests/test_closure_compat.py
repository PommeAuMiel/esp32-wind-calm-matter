"""Exercise both original compatibility shims with C++20 optional comparisons."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FAKE = r'''
#pragma once
#include <optional>
namespace chip::app::Clusters::ClosureControl {
namespace Structs {
struct OverallCurrentStateStruct { struct Type {
  std::optional<int> position{}, latch{}, speed{}, secureState{};
}; };
struct OverallTargetStateStruct { struct Type {
  std::optional<int> position{}, latch{}, speed{};
}; };
}
inline int comparisons = 0;
struct GenericOverallCurrentState : Structs::OverallCurrentStateStruct::Type {
 bool operator==(const Structs::OverallCurrentStateStruct::Type &rhs) const {
  ++comparisons;
  return position == rhs.position && latch == rhs.latch && speed == rhs.speed && secureState == rhs.secureState;
 }
};
struct GenericOverallTargetState : Structs::OverallTargetStateStruct::Type {
 bool operator==(const Structs::OverallTargetStateStruct::Type &rhs) const {
  ++comparisons;
  return position == rhs.position && latch == rhs.latch && speed == rhs.speed;
 }
};
}
'''


class ClosureCompatibilityTests(unittest.TestCase):
    def test_sdk_comparison_is_delegated_with_optional_and_nullable_fields(self):
        compiler = shutil.which("clang++") or shutil.which("g++")
        self.assertIsNotNone(compiler)
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            header = temp / "app/clusters/closure-control-server/closure-control-cluster-objects.h"
            header.parent.mkdir(parents=True)
            header.write_text(FAKE)
            for profile in ("WindCalmThreadSDK", "WindCalmThreadOTA"):
                with self.subTest(profile=profile):
                    source = '#include "firmware/' + profile + '/patches/matter_closure_patch.h"\n' + r'''
#include <cassert>
using namespace chip::app::Clusters::ClosureControl;
int main() {
 std::optional<GenericOverallCurrentState> a, b;
 assert(a == b); a.emplace(); assert(a != b); b.emplace(); assert(a == b);
 b->secureState = 1; assert(a != b); a->secureState = 1; assert(a == b);
 b->latch = 2; assert(a != b); a->latch = 2; assert(a == b);
 b->position = 3; assert(a != b); a->position = 3; assert(a == b);
 b->speed = 4; assert(a != b); a->speed = 4; assert(a == b);
 std::optional<GenericOverallTargetState> c, d;
 c.emplace(); d.emplace(); assert(c == d);
 d->position = 5; assert(c != d); c->position = 5; assert(c == d);
 d->latch = 6; assert(c != d); c->latch = 6; assert(c == d);
 d->speed = 7; assert(c != d); c->speed = 7; assert(c == d);
 assert(comparisons >= 15);
}
'''
                    file = temp / "compare.cpp"
                    file.write_text(source)
                    exe = temp / "closure-test"
                    subprocess.run([compiler, "-std=c++20", "-Wall", "-Wextra", "-Werror",
                                    "-fsanitize=address,undefined", "-I", str(temp), "-I", str(ROOT),
                                    str(file), "-o", str(exe)], check=True)
                    subprocess.run([str(exe)], check=True)


if __name__ == "__main__":
    unittest.main()
