/*
 * Copyright (c) Facebook, Inc. and its affiliates.
 *
 * This software may be used and distributed according to the terms of the
 * GNU General Public License version 2.
 */

use std::env::var;
use std::io as std_io;
use std::net::{IpAddr, SocketAddr};
use std::time::Duration;

use anyhow::{bail, format_err, Error, Result};
use bytes::Bytes;
use context::generate_session_id;
use fbinit::FacebookInit;
use futures_old::{future, stream, Future, Sink, Stream};
use hostname::get_hostname;
use slog::{debug, error, o, Drain, Logger};

use dns_lookup::lookup_addr;
use libc::c_ulong;
use openssl::ssl::{SslConnector, SslMethod, SslVerifyMode};
use tokio_io::codec::{FramedRead, FramedWrite};
use tokio_io::AsyncRead;
use tokio_openssl::{SslConnectorExt, SslStream};
use users::get_current_username;

use tokio::net::TcpStream;
use tokio::util::FutureExt as TokioFutureExt;

use clap::ArgMatches;

use failure_ext::{err_downcast_ref, SlogKVError};
use futures::compat::Future01CompatExt;
use futures_ext::StreamExt;
use futures_stats::TimedFutureExt;
use scuba_ext::{ScubaSampleBuilder, ScubaSampleBuilderExt};
use secure_utils::{build_identity, read_x509};
use sshrelay::{
    Preamble, Priority, SenderBytesWrite, SshDecoder, SshEncoder, SshEnvVars, SshMsg, SshStream,
    Stdio,
};

mod fdio;

const X509_R_CERT_ALREADY_IN_HASH_TABLE: c_ulong = 185057381;

// Wait for up to 1sec to let Scuba flush its data to the server.
const SCUBA_TIMEOUT: Duration = Duration::from_millis(1000);

pub async fn cmd(
    fb: FacebookInit,
    main: &ArgMatches<'_>,
    sub: &ArgMatches<'_>,
) -> Result<(), Error> {
    if sub.is_present("stdio") {
        if let Some(repo) = main.value_of("repository") {
            let query_string = main.value_of("query-string").unwrap_or("");
            let mononoke_path = sub.value_of("mononoke-path").unwrap();
            let priority = main
                .value_of("priority")
                .map(|p| p.parse())
                .transpose()?
                .unwrap_or(Priority::Default);
            let show_session_output = !main.is_present("no-session-output");

            let cert = sub
                .value_of("cert")
                .expect("certificate file is not specified");
            let private_key = sub
                .value_of("private-key")
                .expect("private key file is not specified");
            let ca_pem = sub
                .value_of("ca-pem")
                .expect("Cental authority pem file is not specified");
            let common_name = sub
                .value_of("common-name")
                .expect("expected SSL common name of the Mononoke server");
            let insecure = sub.is_present("insecure");
            let is_remote_proxy = main.is_present("remote-proxy");
            let scuba_table = main.value_of("scuba-table");
            let mock_username = sub.value_of("mock-username");

            return StdioRelay {
                fb,
                path: mononoke_path,
                repo,
                query_string,
                cert,
                private_key,
                ca_pem,
                ssl_common_name: common_name,
                insecure,
                is_remote_proxy,
                scuba_table,
                mock_username,
                show_session_output,
                priority,
            }
            .run()
            .await;
        }
        return Err(format_err!("Missing repository"));
    }
    return Err(format_err!("Only stdio server is supported"));
}

struct StdioRelay<'a> {
    fb: FacebookInit,
    path: &'a str,
    repo: &'a str,
    query_string: &'a str,
    cert: &'a str,
    private_key: &'a str,
    ca_pem: &'a str,
    ssl_common_name: &'a str,
    insecure: bool,
    is_remote_proxy: bool,
    scuba_table: Option<&'a str>,
    mock_username: Option<&'a str>,
    show_session_output: bool,
    priority: Priority,
}

impl<'a> StdioRelay<'a> {
    async fn run(self) -> Result<(), Error> {
        let mut scuba_logger =
            ScubaSampleBuilder::with_opt_table(self.fb, self.scuba_table.map(|v| v.to_owned()));

        let session_uuid = generate_session_id();
        let unix_username = if let Some(mock_username) = self.mock_username {
            Some(mock_username.to_string())
        } else {
            get_current_username().and_then(|os_str| os_str.into_string().ok())
        };
        let source_hostname = if self.is_remote_proxy {
            // hgcli is run as remote proxy so grab from ssh the information about what host has
            // connected to this proxy and save it as source_hostname
            var("SSH_CONNECTION")
                .ok()
                .and_then(|line| {
                    line.split_whitespace()
                        .next()
                        .and_then(|ip| ip.parse::<IpAddr>().ok())
                })
                .and_then(|ip| lookup_addr(&ip).ok())
        } else {
            // hgcli is run locally, so the source_hostname is the host it is currently running on
            get_hostname().ok()
        };

        let mut preamble = Preamble::new(
            self.repo.to_owned(),
            session_uuid.clone(),
            unix_username,
            source_hostname,
            SshEnvVars::new_from_env(),
        );

        self.priority.add_to_preamble(&mut preamble);

        scuba_logger.add_preamble(&preamble);

        let stdio = Stdio {
            preamble,
            stdin: fdio::stdin(),
            stdout: fdio::stdout(),
            stderr: fdio::stderr(),
        };

        let client_logger = {
            let stderr_write = SenderBytesWrite {
                chan: stdio.stderr.clone().wait(),
            };
            let drain = slog_term::PlainSyncDecorator::new(stderr_write);
            let drain = slog_term::FullFormat::new(drain).build();
            Logger::root(drain.ignore_res(), o!())
        };

        if self.show_session_output {
            // This message is parsed on various places by Sandcastle to determine it was served by
            // Mononoke. This message should remain exactly like this, therefor we serve Sandcastle
            // and use the fallback scenario for when query string is empty to show this message. Once
            // hg-ssh-wrapper everywhere is updated to always pass along the query string, we can make
            // this a non-optional parameter and show the user friendly message on empty query string.
            if self.query_string.is_empty() || self.query_string.contains("sandcastle") {
                debug!(
                    client_logger,
                    "Session with Mononoke started with uuid: {}", session_uuid
                );
            } else {
                eprintln!("server: https://fburl.com/mononoke");
                eprintln!("session: {}", session_uuid);
            }
        }

        scuba_logger.log_with_msg("Hgcli proxy - Connected", None);

        let (stats, result) = self.internal_run(stdio).timed().await;
        scuba_logger.add_future_stats(&stats);
        match result {
            Ok(_) => scuba_logger.log_with_msg("Hgcli proxy - Success", None),
            Err(err) => {
                scuba_logger.log_with_msg("Hgcli proxy - Failure", format!("{:#?}", err));
                error!(client_logger, "Error in hgcli proxy"; SlogKVError(err));
            }
        }
        scuba_logger.flush(SCUBA_TIMEOUT);
        Ok(())
    }

    async fn establish_connection(&self) -> Result<SslStream<TcpStream>, Error> {
        let path = self.path.to_owned();
        let ssl_common_name = self.ssl_common_name.to_owned();

        let connector = {
            let mut connector = SslConnector::builder(SslMethod::tls())?;

            if self.insecure {
                connector.set_verify(SslVerifyMode::NONE);
            }

            let pkcs12 = build_identity(self.cert.to_owned(), self.private_key.to_owned())?;
            connector.set_certificate(&pkcs12.cert)?;
            connector.set_private_key(&pkcs12.pkey)?;

            // add root certificate

            connector
                .cert_store_mut()
                .add_cert(read_x509(self.ca_pem)?)
                .or_else(|err| {
                    let mut failed = true;
                    {
                        let errors = err.errors();
                        if errors.len() == 1 {
                            if errors[0].code() == X509_R_CERT_ALREADY_IN_HASH_TABLE {
                                // Do not fail if certificate has already been added since it's
                                // not really an error
                                failed = false;
                            }
                        }
                    }
                    if failed {
                        let err: Error = err.into();
                        Err(err)
                    } else {
                        Ok(())
                    }
                })?;

            connector.build()
        };

        let addr: SocketAddr = path.parse()?;
        TcpStream::connect(&addr)
            .map_err(|err| format_err!("connecting to Mononoke {} socket '{}' failed", path, err))
            .and_then(move |socket| {
                let async_connector = connector
                    .connect_async(&ssl_common_name, socket)
                    .timeout(Duration::from_secs(15));
                async_connector.map_err(|err| format_err!("async connect error {}", err))
            })
            .compat()
            .await
    }

    async fn internal_run(self, stdio: Stdio) -> Result<(), Error> {
        let Stdio {
            preamble,
            stdin,
            stdout,
            stderr,
        } = stdio;

        let socket = self.establish_connection().await?;

        // Wrap the socket with the ssh codec
        let (socket_read, socket_write) = socket.split();
        let rx = FramedRead::new(socket_read, SshDecoder::new());
        let tx = FramedWrite::new(socket_write, SshEncoder::new());

        let preamble = stream::once(Ok(SshMsg::new(SshStream::Preamble(preamble), Bytes::new())));

        // Start a task to copy from stdin to the socket
        let stdin_future = preamble
            .chain(stdin.map(|buf| SshMsg::new(SshStream::Stdin, buf)))
            .forward(tx)
            .map_err(Error::from)
            .map(|_| ());

        // A task to copy from the socket, then use streamfork() to split the
        // input between stdout and stderr.
        let stdout_future = rx
            .streamfork(
                // a sink each for stdout and stderr, prefixed with With to remove the
                // SshMsg framing and expose the raw data
                stdout.with(|m| future::ok::<_, Error>(SshMsg::data(m))),
                stderr.with(|m| future::ok::<_, Error>(SshMsg::data(m))),
                |msg| -> Result<bool> {
                    // Select a sink based on the stream
                    match msg.stream() {
                        SshStream::Stdout => Ok(false),
                        SshStream::Stderr => Ok(true),
                        bad => bail!("Bad stream: {:?}", bad),
                    }
                },
            )
            .map(|_| ());

        stdout_future
            .then(|res| {
                match res {
                    Ok(res) => Ok(res),
                    Err(err) => {
                        // TODO(stash): T39586884 "Connection reset" can happen in case
                        // of error on the Mononoke server
                        let res = err_downcast_ref!(
                            err,
                            ioerr: std_io::Error => ioerr.kind() == ::std::io::ErrorKind::ConnectionReset,
                        );
                        match res {
                            Some(true) => Ok(()),
                            _ => Err(err),
                        }
                    }
                }
            })
            .select(stdin_future)
            .map(|_| ())
            .map_err(|(err, _)| err)
            .compat().await
    }
}
