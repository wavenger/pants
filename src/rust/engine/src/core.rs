// Copyright 2017 Pants project contributors (see CONTRIBUTORS.md).
// Licensed under the Apache License, Version 2.0 (see LICENSE).

use fnv::FnvHasher;

use std::ops::Deref;
use std::sync::Arc;
use std::{fmt, hash};

use crate::externs;
use crate::handles::Handle;

use rule_graph;
use smallvec::{smallvec, SmallVec};

pub type FNV = hash::BuildHasherDefault<FnvHasher>;

///
/// Params represent a TypeId->Key map.
///
/// For efficiency and hashability, they're stored as sorted Keys (with distinct TypeIds), and
/// wrapped in an `Arc` that allows us to copy-on-write for param contents.
///
#[repr(C)]
#[derive(Clone, Debug, Default, Eq, Hash, PartialEq)]
pub struct Params(SmallVec<[Key; 4]>);

impl Params {
  pub fn new<I: IntoIterator<Item = Key>>(param_inputs: I) -> Result<Params, String> {
    let mut params = param_inputs.into_iter().collect::<SmallVec<[Key; 4]>>();
    params.sort_by_key(|k| *k.type_id());

    if params.len() > 1 {
      let mut prev = &params[0];
      for param in &params[1..] {
        if param.type_id() == prev.type_id() {
          return Err(format!(
            "Values used as `Params` must have distinct types, but the following values had the same type (`{}`):\n  {}\n  {}",
            externs::type_to_str(*prev.type_id()),
            externs::key_to_str(prev),
            externs::key_to_str(param)
          ));
        }
        prev = param;
      }
    }

    Ok(Params(params))
  }

  pub fn new_single(param: Key) -> Params {
    Params(smallvec![param])
  }

  ///
  /// Adds the given param Key to these Params, replacing an existing param with the same type if
  /// it exists.
  ///
  pub fn put(&mut self, param: Key) {
    match self.binary_search(param.type_id) {
      Ok(idx) => self.0[idx] = param,
      Err(idx) => self.0.insert(idx, param),
    }
  }

  ///
  /// Filters this Params object in-place to contain only params matching the given predicate.
  ///
  pub fn retain<F: FnMut(&mut Key) -> bool>(&mut self, f: F) {
    self.0.retain(f)
  }

  ///
  /// Returns the Key for the given TypeId if it is represented in this set of Params.
  ///
  pub fn find(&self, type_id: TypeId) -> Option<&Key> {
    self.binary_search(type_id).ok().map(|idx| &self.0[idx])
  }

  fn binary_search(&self, type_id: TypeId) -> Result<usize, usize> {
    self
      .0
      .binary_search_by(|probe| probe.type_id().cmp(&type_id))
  }

  pub fn type_ids<'a>(&'a self) -> impl Iterator<Item = TypeId> + 'a {
    self.0.iter().map(|k| *k.type_id())
  }

  ///
  /// Given a set of either param type or param value strings: sort, join, and render as one string.
  ///
  pub fn display<T>(params: T) -> String
  where
    T: Iterator,
    T::Item: fmt::Display,
  {
    let mut params: Vec<_> = params.map(|p| format!("{}", p)).collect();
    match params.len() {
      0 => "()".to_string(),
      1 => params.pop().unwrap(),
      _ => {
        params.sort();
        format!("({})", params.join(", "))
      }
    }
  }
}

impl fmt::Display for Params {
  fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
    write!(f, "{}", Self::display(self.0.iter()))
  }
}

pub type Id = u64;

// The type of a python object (which itself has a type, but which is not represented
// by a Key, because that would result in a infinitely recursive structure.)
#[repr(C)]
#[derive(Clone, Copy, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct TypeId(pub Id);

impl TypeId {
  fn pretty_print(self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
    if self == ANY_TYPE {
      write!(f, "Any")
    } else {
      write!(f, "{}", externs::type_to_str(self))
    }
  }
}

impl rule_graph::TypeId for TypeId {
  ///
  /// Render a string for a collection of TypeIds.
  ///
  fn display<I>(type_ids: I) -> String
  where
    I: Iterator<Item = TypeId>,
  {
    Params::display(type_ids)
  }
}

impl fmt::Debug for TypeId {
  fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
    self.pretty_print(f)
  }
}

impl fmt::Display for TypeId {
  fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
    self.pretty_print(f)
  }
}

// On the python side, the 0th type id is used as an anonymous id
pub const ANY_TYPE: TypeId = TypeId(0);

// An identifier for a python function.
#[repr(C)]
#[derive(Clone, Copy, Eq, Hash, PartialEq)]
pub struct Function(pub Key);

impl Function {
  pub fn name(&self) -> String {
    let Function(key) = self;
    let module = externs::project_str(&externs::val_for(&key), "__module__");
    let name = externs::project_str(&externs::val_for(&key), "__name__");
    // NB: this is a custom dunder method that Python code should populate before sending the
    // function (e.g. an `@rule`) through FFI.
    let line_number = externs::project_str(&externs::val_for(&key), "__line_number__");
    format!("{}:{}:{}", module, line_number, name)
  }
}

impl fmt::Display for Function {
  fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
    write!(f, "{}()", self.name())
  }
}

impl fmt::Debug for Function {
  fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
    write!(f, "{}()", self.name())
  }
}

///
/// Wraps a type id for use as a key in HashMaps and sets.
///
#[repr(C)]
#[derive(Clone, Copy)]
pub struct Key {
  id: Id,
  type_id: TypeId,
}

impl fmt::Debug for Key {
  fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
    write!(f, "{}", externs::key_to_str(self))
  }
}

impl Eq for Key {}

impl PartialEq for Key {
  fn eq(&self, other: &Key) -> bool {
    self.id == other.id
  }
}

impl hash::Hash for Key {
  fn hash<H: hash::Hasher>(&self, state: &mut H) {
    self.id.hash(state);
  }
}

impl fmt::Display for Key {
  fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
    write!(f, "{}", externs::key_to_str(self))
  }
}

impl Key {
  pub fn new(id: Id, type_id: TypeId) -> Key {
    Key { id, type_id }
  }

  pub fn id(&self) -> Id {
    self.id
  }

  pub fn type_id(&self) -> &TypeId {
    &self.type_id
  }
}

///
/// A wrapper around a Arc<Handle>
///
#[derive(Clone, Eq, PartialEq)]
pub struct Value(Arc<Handle>);

impl Value {
  pub fn new(handle: Handle) -> Value {
    Value(Arc::new(handle))
  }
}

impl Deref for Value {
  type Target = Handle;

  fn deref(&self) -> &Handle {
    &self.0
  }
}

impl fmt::Debug for Value {
  fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
    write!(f, "{}", externs::val_to_str(&self))
  }
}

///
/// Creates a Handle (which represents exclusive access) from a Value (which might be shared),
/// cloning if necessary.
///
impl From<Value> for Handle {
  fn from(value: Value) -> Self {
    match Arc::try_unwrap(value.0) {
      Ok(handle) => handle,
      Err(arc_handle) => externs::clone_val(&arc_handle),
    }
  }
}

impl From<Handle> for Value {
  fn from(handle: Handle) -> Self {
    Value::new(handle)
  }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum Failure {
  /// A Node failed because a filesystem change invalidated it or its inputs.
  /// A root requestor should usually immediately retry their request.
  Invalidated,
  /// An error was thrown.
  Throw {
    // A python exception value, which might have a python-level stacktrace
    val: Value,
    // A pre-formatted python exception traceback.
    python_traceback: String,
    // A stack of engine-side "frame" information generated from Nodes.
    engine_traceback: Vec<String>,
  },
}

impl Failure {
  ///
  /// Consumes this Failure to produce a new Failure with an additional engine_traceback entry.
  ///
  pub fn with_pushed_frame(self, frame: &impl fmt::Display) -> Failure {
    match self {
      Failure::Invalidated => Failure::Invalidated,
      Failure::Throw {
        val,
        python_traceback,
        mut engine_traceback,
      } => {
        engine_traceback.push(format!("{}", frame));
        Failure::Throw {
          val,
          python_traceback,
          engine_traceback,
        }
      }
    }
  }
}

impl fmt::Display for Failure {
  fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
    match self {
      Failure::Invalidated => write!(f, "Giving up on retrying due to changed files."),
      Failure::Throw { val, .. } => write!(f, "{}", externs::val_to_str(val)),
    }
  }
}

pub fn native_python_traceback(msg: &str) -> String {
  format!(
    "Traceback (no traceback):\n  <pants native internals>\nException: {}",
    msg
  )
}

pub fn throw(msg: &str) -> Failure {
  Failure::Throw {
    val: externs::create_exception(msg),
    python_traceback: native_python_traceback(msg),
    engine_traceback: Vec::new(),
  }
}
