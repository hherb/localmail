//! Writing a downloaded file to the path the user chose (#465).
//!
//! `std::fs::write` truncates the destination and then writes, so a failure
//! partway through (a full disk) leaves a truncated file that looks like a
//! finished download — and if the user chose to overwrite an existing file,
//! that file is already gone. [`write_replacing`] writes a temporary file
//! beside the destination and renames it over the destination only once
//! every byte is on disk; on any failure the temporary is removed and the
//! destination is untouched.

use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

/// Write `bytes` to `dest`, replacing any file there, so that `dest` holds
/// either its old contents or all of `bytes` — never a prefix.
///
/// shortcut: the rename replaces the directory entry, so an overwritten
/// file's permissions are not carried over and a symlink at `dest` is
/// replaced rather than followed; copy the metadata first if that matters.
pub fn write_replacing(dest: &Path, bytes: &[u8]) -> io::Result<()> {
    replace_with(dest, |file| file.write_all(bytes))
}

/// [`write_replacing`] with the write itself supplied, so a test can fail
/// it partway — the full-disk case no fixture can provoke portably.
fn replace_with(dest: &Path, write: impl FnOnce(&mut File) -> io::Result<()>) -> io::Result<()> {
    let tmp = temp_path(dest)?;
    // `create_new`, so a name we did not mint is never written or removed.
    let mut file = OpenOptions::new().write(true).create_new(true).open(&tmp)?;
    let result = write(&mut file)
        .and_then(|()| file.sync_all())
        .and_then(|()| fs::rename(&tmp, dest));
    if result.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    result
}

/// A hidden sibling of `dest`, unique to this process and moment. Same
/// directory, so the rename never crosses a filesystem.
fn temp_path(dest: &Path) -> io::Result<PathBuf> {
    let name = dest.file_name().ok_or_else(|| {
        io::Error::new(io::ErrorKind::InvalidInput, "destination has no file name")
    })?;
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let mut tmp = std::ffi::OsString::from(".");
    tmp.push(name);
    tmp.push(format!(".{}.{nanos}.part", std::process::id()));
    Ok(dest.with_file_name(tmp))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A fresh empty directory under the system temp dir, removed on drop.
    struct Scratch(PathBuf);
    impl Scratch {
        fn new(tag: &str) -> Self {
            let dir = std::env::temp_dir().join(format!(
                "localmail-465-{tag}-{}-{}",
                std::process::id(),
                SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap()
                    .as_nanos()
            ));
            fs::create_dir(&dir).unwrap();
            Scratch(dir)
        }
        fn entries(&self) -> Vec<String> {
            let mut names: Vec<String> = fs::read_dir(&self.0)
                .unwrap()
                .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
                .collect();
            names.sort();
            names
        }
    }
    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn a_new_file_gets_every_byte_and_no_temp_is_left() {
        let dir = Scratch::new("new");
        let dest = dir.0.join("invoice.pdf");
        write_replacing(&dest, b"hello").unwrap();
        assert_eq!(fs::read(&dest).unwrap(), b"hello");
        assert_eq!(dir.entries(), ["invoice.pdf"]);
    }

    #[test]
    fn an_existing_file_is_replaced_whole() {
        let dir = Scratch::new("replace");
        let dest = dir.0.join("a.txt");
        fs::write(&dest, b"old contents, longer than the new").unwrap();
        write_replacing(&dest, b"new").unwrap();
        assert_eq!(fs::read(&dest).unwrap(), b"new");
        assert_eq!(dir.entries(), ["a.txt"]);
    }

    // The #465 defect: a failure after the write started. A non-empty
    // directory at `dest` makes the rename fail once every byte is written,
    // which exercises the same clean-up a full disk would.
    #[test]
    fn a_failed_replace_removes_the_temp_and_leaves_dest_alone() {
        let dir = Scratch::new("fail");
        let dest = dir.0.join("taken");
        fs::create_dir(&dest).unwrap();
        fs::write(dest.join("keep"), b"precious").unwrap();
        assert!(write_replacing(&dest, b"new bytes").is_err());
        assert_eq!(dir.entries(), ["taken"], "the temp must not survive");
        assert_eq!(fs::read(dest.join("keep")).unwrap(), b"precious");
    }

    // The #465 defect itself: the write fails after some bytes are out
    // (a full disk). `fs::write` would have truncated the old file already.
    #[test]
    fn a_write_failing_partway_keeps_the_old_file_and_no_temp() {
        let dir = Scratch::new("partial");
        let dest = dir.0.join("chosen.pdf");
        fs::write(&dest, b"the file the user chose to overwrite").unwrap();
        let result = replace_with(&dest, |f| {
            f.write_all(b"half of the")?;
            Err(io::Error::other("no space left on device"))
        });
        assert!(result.is_err());
        assert_eq!(
            fs::read(&dest).unwrap(),
            b"the file the user chose to overwrite"
        );
        assert_eq!(dir.entries(), ["chosen.pdf"], "the temp must not survive");
    }

    #[test]
    fn a_destination_without_a_file_name_is_refused() {
        let err = write_replacing(Path::new("/"), b"x").unwrap_err();
        assert_eq!(err.kind(), io::ErrorKind::InvalidInput);
    }

    #[test]
    fn the_temp_is_a_hidden_sibling_of_dest() {
        let tmp = temp_path(Path::new("/d/report.pdf")).unwrap();
        assert_eq!(tmp.parent(), Some(Path::new("/d")));
        let name = tmp.file_name().unwrap().to_string_lossy().into_owned();
        assert!(
            name.starts_with(".report.pdf.") && name.ends_with(".part"),
            "{name}"
        );
    }
}
