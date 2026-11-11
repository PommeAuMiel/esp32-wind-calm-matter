#pragma once
// SPDX-License-Identifier: MIT
// Original compatibility shim: call the SDK's existing comparison explicitly.
// This avoids C++20 rewritten-candidate ambiguity for optional<derived type>.
// The SDK implementation, including nullable/optional semantics, stays in charge.
#ifdef __cplusplus
#include <app/clusters/closure-control-server/closure-control-cluster-objects.h>
namespace chip::app::Clusters::ClosureControl {
inline bool operator==(const GenericOverallCurrentState &lhs, const GenericOverallCurrentState &rhs) {
  return lhs.operator==(static_cast<const Structs::OverallCurrentStateStruct::Type &>(rhs));
}
inline bool operator==(const GenericOverallTargetState &lhs, const GenericOverallTargetState &rhs) {
  return lhs.operator==(static_cast<const Structs::OverallTargetStateStruct::Type &>(rhs));
}
} // namespace chip::app::Clusters::ClosureControl
#endif
